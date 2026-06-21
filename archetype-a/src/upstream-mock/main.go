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
	Status   string `json:"status"`
	LedgerID int64  `json:"ledger_id,omitempty"`
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

		// Simulate slow upstream commit (Postgres write completes after delay).
		time.Sleep(commitLatency)

		var ledgerID int64
		err := db.QueryRow(
			`INSERT INTO ledger (payment_id, amount, idempotency_key)
			 VALUES ($1, $2, NULL)
			 RETURNING id`,
			req.PaymentID,
			req.Amount,
		).Scan(&ledgerID)
		if err != nil {
			log.Printf("ledger insert failed: %v", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(chargeResponse{Status: "ok", LedgerID: ledgerID})
	})

	addr := ":8081"
	log.Printf("upstream-mock listening on %s (commit_latency=%s)", addr, commitLatency)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatal(err)
	}
}
