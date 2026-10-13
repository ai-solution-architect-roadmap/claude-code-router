package main

import (
	"fmt"

	"example.com/shop/internal/store"
)

func main() {
	s := store.New()
	fmt.Println(helper(s))
}
