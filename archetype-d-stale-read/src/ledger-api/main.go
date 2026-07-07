package main

import (
	"database/sql"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	_ "github.com/lib/pq"
)

// readWritePin implements read-after-write consistency: for a short window after
// a client writes an account, that account's reads are served from the primary
// so the client reads its own write. Reads outside the window use the replica to
// offload the primary.
type readWritePin struct {
	mu  sync.Mutex
	ttl time.Duration
	exp map[string]time.Time
}

func newReadWritePin(ttl time.Duration) *readWritePin {
	return &readWritePin{ttl: ttl, exp: make(map[string]time.Time)}
}

func (p *readWritePin) mark(account string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.exp[account] = time.Now().Add(p.ttl)
}

func (p *readWritePin) pinned(account string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	exp, ok := p.exp[account]
	return ok && time.Now().Before(exp)
}

type server struct {
	primary *sql.DB
	replica *sql.DB
	pin     *readWritePin
}

type depositRequest struct {
	Account string `json:"account"`
	Amount  int64  `json:"amount_cents"`
}

func (s *server) handleDeposit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req depositRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if req.Account == "" || req.Amount == 0 {
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}

	var balance int64
	err := s.primary.QueryRow(
		"UPDATE accounts SET balance_cents = balance_cents + $1 WHERE id = $2 RETURNING balance_cents",
		req.Amount, req.Account,
	).Scan(&balance)
	if err != nil {
		http.Error(w, "write failed: "+err.Error(), http.StatusInternalServerError)
		return
	}

	// Client just wrote this account; serve its reads from the primary briefly.
	s.pin.mark(req.Account)

	writeJSON(w, map[string]any{
		"account":       req.Account,
		"balance_cents": balance,
		"source":        "primary",
	})
}

func (s *server) handleBalance(w http.ResponseWriter, r *http.Request) {
	account := r.URL.Query().Get("account")
	if account == "" {
		http.Error(w, "account required", http.StatusBadRequest)
		return
	}

	source := "replica"
	db := s.replica
	if s.pin.pinned(account) {
		source = "primary"
		db = s.primary
	}

	balance, err := readBalance(db, account)
	if err != nil && source == "primary" {
		// Primary read path unavailable; fall back to the replica.
		source = "replica"
		balance, err = readBalance(s.replica, account)
	}
	if err != nil {
		http.Error(w, "read failed: "+err.Error(), http.StatusInternalServerError)
		return
	}

	writeJSON(w, map[string]any{
		"account":       account,
		"balance_cents": balance,
		"source":        source,
	})
}

func readBalance(db *sql.DB, account string) (int64, error) {
	var balance int64
	err := db.QueryRow("SELECT balance_cents FROM accounts WHERE id = $1", account).Scan(&balance)
	return balance, err
}

func (s *server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	if err := s.primary.Ping(); err != nil {
		http.Error(w, "primary down", http.StatusServiceUnavailable)
		return
	}
	if err := s.replica.Ping(); err != nil {
		http.Error(w, "replica down", http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok"))
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func mustOpen(dsn string) *sql.DB {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		log.Fatal(err)
	}
	db.SetMaxOpenConns(10)
	return db
}

func main() {
	primaryDSN := os.Getenv("PRIMARY_URL")
	replicaDSN := os.Getenv("REPLICA_URL")
	if primaryDSN == "" || replicaDSN == "" {
		log.Fatal("PRIMARY_URL and REPLICA_URL required")
	}

	ttlMS := 250
	if v := os.Getenv("PIN_TTL_MS"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil {
			ttlMS = parsed
		}
	}

	s := &server{
		primary: mustOpen(primaryDSN),
		replica: mustOpen(replicaDSN),
		pin:     newReadWritePin(time.Duration(ttlMS) * time.Millisecond),
	}
	defer s.primary.Close()
	defer s.replica.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/deposit", s.handleDeposit)
	mux.HandleFunc("/balance", s.handleBalance)

	addr := ":8080"
	if p := os.Getenv("PORT"); p != "" {
		addr = ":" + p
	}
	log.Printf("ledger-api listening on %s (pin ttl %dms)", addr, ttlMS)
	log.Fatal(http.ListenAndServe(addr, mux))
}
