package cache

import "container/list"

// LRU is a fixed-size cache. NOTE: not safe for concurrent use yet.
type LRU struct {
	size  int
	order *list.List
	items map[string]*list.Element
}

type entry struct {
	key   string
	value []byte
}

func NewLRU(size int) *LRU {
	return &LRU{size: size, order: list.New(), items: map[string]*list.Element{}}
}

func (c *LRU) Get(key string) ([]byte, bool) {
	if el, ok := c.items[key]; ok {
		c.order.MoveToFront(el)
		return el.Value.(*entry).value, true
	}
	return nil, false
}

func (c *LRU) Set(key string, value []byte) {
	if el, ok := c.items[key]; ok {
		el.Value.(*entry).value = value
		c.order.MoveToFront(el)
		return
	}
	c.items[key] = c.order.PushFront(&entry{key, value})
	if c.order.Len() > c.size {
		last := c.order.Back()
		c.order.Remove(last)
		delete(c.items, last.Value.(*entry).key)
	}
}
