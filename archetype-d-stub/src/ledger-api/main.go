package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	_ "github.com/lib/pq"
)

type transferRequest struct {
	FromAccount string `json:"from_account"`
	Amount      int    `json:"amount_cents"`
}

func main() {
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		log.Fatal("DATABASE_URL required")
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/transfer", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req transferRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad json", http.StatusBadRequest)
			return
		}
		if req.FromAccount == "" || req.Amount <= 0 {
			http.Error(w, "invalid request", http.StatusBadRequest)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		if err := processTransfer(ctx, db, req.FromAccount, req.Amount); err != nil {
			http.Error(w, err.Error(), http.StatusConflict)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("/balances", func(w http.ResponseWriter, r *http.Request) {
		rows, err := db.QueryContext(r.Context(), "SELECT id, balance_cents FROM accounts ORDER BY id")
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		defer rows.Close()
		out := make(map[string]int)
		for rows.Next() {
			var id string
			var bal int
			if err := rows.Scan(&id, &bal); err != nil {
				http.Error(w, "scan failed", http.StatusInternalServerError)
				return
			}
			out[id] = bal
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(out)
	})

	addr := ":8080"
	if p := os.Getenv("PORT"); p != "" {
		addr = ":" + p
	}
	log.Printf("ledger-api listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}

// processTransfer debits from_account after verifying this debit keeps the pool
// above the configured reserve (enforced in application code only).
func processTransfer(ctx context.Context, db *sql.DB, fromAccount string, amount int) error {
	const poolReserveCents = 8000

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var balA, balB int
	err = tx.QueryRowContext(
		ctx,
		`SELECT
			(SELECT balance_cents FROM accounts WHERE id = 'pool-a'),
			(SELECT balance_cents FROM accounts WHERE id = 'pool-b')`,
	).Scan(&balA, &balB)
	if err != nil {
		return fmt.Errorf("pool lookup failed: %w", err)
	}
	if balA+balB-amount < poolReserveCents {
		return fmt.Errorf(
			"transfer would breach pool reserve: after debit total %d < reserve %d",
			balA+balB-amount,
			poolReserveCents,
		)
	}

	var current int
	err = tx.QueryRowContext(
		ctx,
		"SELECT balance_cents FROM accounts WHERE id = $1",
		fromAccount,
	).Scan(&current)
	if err != nil {
		return fmt.Errorf("account lookup failed: %w", err)
	}
	if current < amount {
		return fmt.Errorf("insufficient account funds: have %d need %d", current, amount)
	}

	_, err = tx.ExecContext(
		ctx,
		"UPDATE accounts SET balance_cents = balance_cents - $1 WHERE id = $2",
		amount,
		fromAccount,
	)
	if err != nil {
		return err
	}
	return tx.Commit()
}
