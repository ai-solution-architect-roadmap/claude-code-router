package store

import "errors"

var ErrNotFound = errors.New("not found")

type Store struct {
	items map[string]string
}

func New() *Store {
	return &Store{items: map[string]string{}}
}

func (s *Store) Get(key string) (string, error) {
	v, ok := s.items[key]
	if !ok {
		return "", ErrNotFound
	}
	return v, nil
}

func (s *Store) Put(key, value string) {
	s.items[key] = value
}
