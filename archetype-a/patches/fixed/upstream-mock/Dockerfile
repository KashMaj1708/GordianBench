FROM golang:1.22-alpine AS build

WORKDIR /app
COPY go.mod ./
RUN go mod download
COPY main.go ./
RUN go mod tidy && CGO_ENABLED=0 go build -o /upstream-mock .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget
COPY --from=build /upstream-mock /upstream-mock
EXPOSE 8081
ENTRYPOINT ["/upstream-mock"]
