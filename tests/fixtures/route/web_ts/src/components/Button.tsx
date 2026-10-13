import React from "react";
import { formatPrice } from "../lib/format";

export interface ButtonProps { label: string; price?: number; onClick?: () => void }

export function Button({ label, price, onClick }: ButtonProps) {
  const text = price === undefined ? label : `${label} (${formatPrice(price)})`;
  return <button style={{ color: "white", background: "green" }} onClick={onClick}>{text}</button>;
}
