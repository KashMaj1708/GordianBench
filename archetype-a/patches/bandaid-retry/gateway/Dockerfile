FROM golang:1.22-alpine AS build

WORKDIR /app
COPY go.mod ./
RUN go mod download || true
COPY main.go ./
RUN go mod tidy && CGO_ENABLED=0 go build -o /gateway .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget
COPY --from=build /gateway /gateway
EXPOSE 8080
ENTRYPOINT ["/gateway"]
