import { describe, expect, it } from "vitest";
import { formatDate, formatPrice } from "../lib/format";

describe("format", () => {
  it("formats prices", () => expect(formatPrice(1200)).toBe("$12.00"));
  it("formats dates", () => expect(formatDate(new Date(0))).toBe("1970-01-01"));
});
