package http

import (
	"net/http"

	"example.com/svc/internal/auth"
	"example.com/svc/internal/cache"
	"example.com/svc/internal/store"
)

func NewRouter(s *store.Store) http.Handler {
	c := cache.NewLRU(128)
	mux := http.NewServeMux()
	mux.HandleFunc("/items/", func(w http.ResponseWriter, r *http.Request) {
		if !auth.Verify(r.URL.Path, r.Header.Get("X-Sig")) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		key := r.URL.Path[len("/items/"):]
		if v, ok := c.Get(key); ok {
			w.Write(v)
			return
		}
		v, err := s.Get(key)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		c.Set(key, []byte(v))
		w.Write([]byte(v))
	})
	return mux
}
