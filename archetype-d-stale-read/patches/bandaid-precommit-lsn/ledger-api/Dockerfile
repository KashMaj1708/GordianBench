FROM golang:1.22-alpine AS build
WORKDIR /app
COPY go.mod go.sum* ./
RUN go mod download 2>/dev/null || true
COPY . .
RUN go mod tidy && CGO_ENABLED=0 go build -o /ledger-api .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget
COPY --from=build /ledger-api /ledger-api
ENV PORT=8080
EXPOSE 8080
CMD ["/ledger-api"]
