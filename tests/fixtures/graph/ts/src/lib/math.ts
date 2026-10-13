export function add(a: number, b: number): number {
  if (a > b && b > 0) {
    return a + b;
  }
  return b;
}
