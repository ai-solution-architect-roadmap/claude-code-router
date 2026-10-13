package store

type Store struct{}

func New() *Store { return &Store{} }

func (s *Store) Get(k string) string {
	switch k {
	case "a":
		return "1"
	default:
		return ""
	}
}
