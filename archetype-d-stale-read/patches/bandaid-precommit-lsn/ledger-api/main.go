package main

// BAND-AID: pre-commit LSN capture (the documented frontier-model error, P3).
//
// This is byte-for-byte the correct LSN fix (src-fixed) EXCEPT for one change in
// handleDeposit: the write watermark is captured with pg_current_wal_lsn() INSIDE
// the writing UPDATE statement (RETURNING ..., pg_current_wal_lsn()) instead of in
// a SEPARATE SELECT after the UPDATE has autocommitted.
//
// Evaluated mid-statement, pg_current_wal_lsn() returns a PRE-COMMIT position at
// or before the LSN the replica has already replayed, so the freshness gate
// `pg_last_wal_replay_lsn() >= need` is trivially satisfied and the read is served
// from the still-stale replica. Every other line (the gate, the routing) is the
// correct mechanism — which is exactly why this is the instructive fixture: the
// ONLY defect is the capture site, and the oracle MUST still FAIL it 10/10.
//
// All four LSN patches from Opus-4.8 (Reports 6+7) made precisely this error.
// Keeping it as a permanent corpus variant guarantees the oracle stays able to
// catch it even if the lag profile is ever retuned (tier2_oracle.py asserts it).

import (
	"database/sql"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"sync"

	_ "github.com/lib/pq"
)

type writeTracker struct {
	mu  sync.Mutex
	lsn map[string]string
}

func newWriteTracker() *writeTracker {
	return &writeTracker{lsn: make(map[string]string)}
}

func (t *writeTracker) mark(account, lsn string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.lsn[account] = lsn
}

func (t *writeTracker) lastLSN(account string) (string, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	lsn, ok := t.lsn[account]
	return lsn, ok
}

type server struct {
	primary *sql.DB
	replica *sql.DB
	tracker *writeTracker
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

	// THE BUG (P3): capture pg_current_wal_lsn() inside the writing statement.
	// This returns a pre-commit position the replica may already have replayed,
	// so the watermark under-reports how far the replica must catch up.
	var balance int64
	var lsn string
	if err := s.primary.QueryRow(
		"UPDATE accounts SET balance_cents = balance_cents + $1 WHERE id = $2 "+
			"RETURNING balance_cents, pg_current_wal_lsn()::text",
		req.Amount, req.Account,
	).Scan(&balance, &lsn); err != nil {
		http.Error(w, "write failed: "+err.Error(), http.StatusInternalServerError)
		return
	}

	s.tracker.mark(req.Account, lsn)

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
	if lsn, ok := s.tracker.lastLSN(account); ok && !s.replicaCaughtUp(lsn) {
		source = "primary"
		db = s.primary
	}

	balance, err := readBalance(db, account)
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

// replicaCaughtUp reports whether the standby has applied at least up to lsn.
func (s *server) replicaCaughtUp(lsn string) bool {
	var caughtUp sql.NullBool
	err := s.replica.QueryRow(
		"SELECT pg_last_wal_replay_lsn() >= $1::pg_lsn", lsn,
	).Scan(&caughtUp)
	if err != nil {
		return false
	}
	return caughtUp.Valid && caughtUp.Bool
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

	s := &server{
		primary: mustOpen(primaryDSN),
		replica: mustOpen(replicaDSN),
		tracker: newWriteTracker(),
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
	log.Printf("ledger-api (bandaid-precommit-lsn) listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}
