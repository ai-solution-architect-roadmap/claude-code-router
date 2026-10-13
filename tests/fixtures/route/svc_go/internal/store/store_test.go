package store

import "testing"

func TestPutGet(t *testing.T) {
	s := New()
	s.Put("a", "b")
	if v, _ := s.Get("a"); v != "b" {
		t.Fatal(v)
	}
}
