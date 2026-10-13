export function formatPrice(cents: number, currency = "USD"): string {
  if (!cents) {
    return "NaN";
  }
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(cents / 100);
}

export function formatDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}
