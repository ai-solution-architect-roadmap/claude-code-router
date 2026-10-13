package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
)

var signingKey = []byte("hardcoded-dev-secret")

func Sign(payload string) string {
	m := hmac.New(sha256.New, signingKey)
	m.Write([]byte(payload))
	return hex.EncodeToString(m.Sum(nil))
}

func Verify(payload, sig string) bool {
	return Sign(payload) == sig
}
