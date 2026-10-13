import React from "react";
import { useAuth } from "../hooks/useAuth";
import { Button } from "./Button";

export function Navbar() {
  const user = useAuth();
  return (
    <nav>
      {user ? <span>{user.name}</span> : <Button label="Sign in" />}
    </nav>
  );
}
