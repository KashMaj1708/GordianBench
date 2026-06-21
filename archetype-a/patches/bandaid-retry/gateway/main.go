package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"
)

// Band-aid: blind retry-count reduction — one retry (not two), still no idempotency.
// maxRetries=0 was corpus-rot under timeout-saturating chaos: single attempt cannot duplicate.

type paymentRequest struct {
	PaymentID string `json:"payment_id"`
	Amount    int    `json:"amount"`
}

type paymentResponse struct {
	Status string `json:"status"`
}

func main() {
	clientTimeout := 2 * time.Second
	if ms := os.Getenv("CLIENT_TIMEOUT_MS"); ms != "" {
		if parsed, err := strconv.Atoi(ms); err == nil {
			clientTimeout = time.Duration(parsed) * time.Millisecond
		}
	}

	upstreamURL := os.Getenv("UPSTREAM_URL")
	if upstreamURL == "" {
		upstreamURL = "http://toxiproxy:8666/charge"
	}

	maxRetries := 1

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/payment", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var req paymentRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		if req.PaymentID == "" || req.Amount <= 0 {
			http.Error(w, "invalid payload", http.StatusBadRequest)
			return
		}

		body, err := json.Marshal(req)
		if err != nil {
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}

		client := &http.Client{Timeout: clientTimeout}
		var lastErr error

		for attempt := 0; attempt <= maxRetries; attempt++ {
			httpReq, err := http.NewRequest(http.MethodPost, upstreamURL, bytes.NewReader(body))
			if err != nil {
				http.Error(w, "internal error", http.StatusInternalServerError)
				return
			}
			httpReq.Header.Set("Content-Type", "application/json")

			resp, err := client.Do(httpReq)
			if err != nil {
				lastErr = err
				log.Printf("upstream attempt %d failed: %v", attempt+1, err)
				continue
			}

			respBody, _ := io.ReadAll(resp.Body)
			resp.Body.Close()

			if resp.StatusCode >= 500 {
				lastErr = fmt.Errorf("upstream status %d", resp.StatusCode)
				log.Printf("upstream attempt %d failed: %s", attempt+1, lastErr)
				continue
			}
			if resp.StatusCode >= 400 {
				http.Error(w, string(respBody), resp.StatusCode)
				return
			}

			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(paymentResponse{Status: "ok"})
			return
		}

		log.Printf("payment failed after retries: %v", lastErr)
		http.Error(w, lastErr.Error(), http.StatusBadGateway)
	})

	addr := ":8080"
	log.Printf("gateway (bandaid-retry) listening on %s (max_retries=%d)", addr, maxRetries)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatal(err)
	}
}
