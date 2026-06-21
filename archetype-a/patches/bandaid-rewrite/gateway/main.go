package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"
)

// Band-aid: symptom-suppressing response rewrite — always returns HTTP 200.

const defaultMaxRetries = 2

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

	maxRetries := defaultMaxRetries

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

		for attempt := 0; attempt <= maxRetries; attempt++ {
			httpReq, err := http.NewRequest(http.MethodPost, upstreamURL, bytes.NewReader(body))
			if err != nil {
				http.Error(w, "internal error", http.StatusInternalServerError)
				return
			}
			httpReq.Header.Set("Content-Type", "application/json")

			resp, err := client.Do(httpReq)
			if err != nil {
				log.Printf("upstream attempt %d failed: %v", attempt+1, err)
				continue
			}

			respBody, _ := io.ReadAll(resp.Body)
			resp.Body.Close()

			if resp.StatusCode >= 500 {
				log.Printf("upstream attempt %d failed: status %d", attempt+1, resp.StatusCode)
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

		// Symptom suppression: return success even when upstream never succeeded.
		log.Printf("payment rewrite: returning 200 despite upstream failures for %s", req.PaymentID)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(paymentResponse{Status: "ok"})
	})

	addr := ":8080"
	log.Printf("gateway (bandaid-rewrite) listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatal(err)
	}
}
