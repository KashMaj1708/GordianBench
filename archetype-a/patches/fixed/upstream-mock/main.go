package main

import (
	"database/sql"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	_ "github.com/lib/pq"
)

type chargeRequest struct {
	PaymentID string `json:"payment_id"`
	Amount    int    `json:"amount"`
}

type chargeResponse struct {
	Status    string `json:"status"`
	LedgerID  int64  `json:"ledger_id,omitempty"`
	Duplicate bool   `json:"duplicate,omitempty"`
}

func lookupExisting(db *sql.DB, idempotencyKey string) (int64, bool) {
	var existingID int64
	err := db.QueryRow(
		`SELECT id FROM ledger WHERE idempotency_key = $1`,
		idempotencyKey,
	).Scan(&existingID)
	if err == nil {
		return existingID, true
	}
	if err != sql.ErrNoRows {
		log.Printf("idempotency lookup failed: %v", err)
	}
	return 0, false
}

func writeOK(w http.ResponseWriter, ledgerID int64, duplicate bool) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(chargeResponse{
		Status:    "ok",
		LedgerID:  ledgerID,
		Duplicate: duplicate,
	})
}

func main() {
	commitLatency := 2200 * time.Millisecond
	if ms := os.Getenv("COMMIT_LATENCY_MS"); ms != "" {
		if parsed, err := strconv.Atoi(ms); err == nil {
			commitLatency = time.Duration(parsed) * time.Millisecond
		}
	}

	dbURL := os.Getenv("DATABASE_URL")
	if dbURL == "" {
		log.Fatal("DATABASE_URL is required")
	}

	db, err := sql.Open("postgres", dbURL)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	defer db.Close()

	for i := 0; i < 30; i++ {
		if err := db.Ping(); err == nil {
			break
		}
		time.Sleep(time.Second)
	}
	if err := db.Ping(); err != nil {
		log.Fatalf("db ping: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/charge", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var req chargeRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if req.PaymentID == "" || req.Amount <= 0 {
			http.Error(w, "invalid payload", http.StatusBadRequest)
			return
		}

		idempotencyKey := r.Header.Get("Idempotency-Key")
		if idempotencyKey == "" {
			http.Error(w, "missing Idempotency-Key", http.StatusBadRequest)
			return
		}

		if existingID, ok := lookupExisting(db, idempotencyKey); ok {
			writeOK(w, existingID, true)
			return
		}

		time.Sleep(commitLatency)

		if existingID, ok := lookupExisting(db, idempotencyKey); ok {
			writeOK(w, existingID, true)
			return
		}

		var ledgerID int64
		err := db.QueryRow(
			`INSERT INTO ledger (payment_id, amount, idempotency_key)
			 VALUES ($1, $2, $3)
			 RETURNING id`,
			req.PaymentID,
			req.Amount,
			idempotencyKey,
		).Scan(&ledgerID)
		if err != nil {
			if existingID, ok := lookupExisting(db, idempotencyKey); ok {
				writeOK(w, existingID, true)
				return
			}
			log.Printf("ledger insert failed: %v", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}

		writeOK(w, ledgerID, false)
	})

	addr := ":8081"
	log.Printf("upstream-mock (fixed) listening on %s (commit_latency=%s)", addr, commitLatency)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatal(err)
	}
}
