import React from "react";
import { Footer } from "./components/Footer";
import { Navbar } from "./components/Navbar";
import { Checkout } from "./pages/Checkout";
import { Home } from "./pages/Home";

export function App() {
  return (
    <>
      <Navbar />
      <Home />
      <Checkout total={1200} />
      <Footer />
    </>
  );
}
