import { useEffect, useState } from "react";
import { request } from "../lib/api";

export interface User { id: string; name: string }

export function useAuth(): User | null {
  const [user, setUser] = useState<User | null>(null);
  useEffect(() => {
    request<User>("/me").then(setUser).catch(() => setUser(null));
  }, []);
  return user;
}
