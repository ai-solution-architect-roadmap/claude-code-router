package main

import (
	"log"
	"net/http"

	apphttp "example.com/svc/internal/http"
	"example.com/svc/internal/store"
)

func main() {
	s := store.New()
	log.Fatal(http.ListenAndServe(":8080", apphttp.NewRouter(s)))
}
