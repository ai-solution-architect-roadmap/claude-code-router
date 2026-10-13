import React, { useState } from "react";
import { Button } from "../components/Button";
import { request } from "../lib/api";
import { formatPrice } from "../lib/format";

export class Checkout extends React.Component<{ total: number }, { busy: boolean }> {
  state = { busy: false };

  pay = async () => {
    this.setState({ busy: true });
    try {
      await request("/checkout", { method: "POST", body: JSON.stringify({ total: this.props.total }) });
    } finally {
      this.setState({ busy: false });
    }
  };

  render() {
    return (
      <div>
        <p>Total: {formatPrice(this.props.total)}</p>
        <Button label={this.state.busy ? "Paying..." : "Pay"} onClick={this.pay} />
      </div>
    );
  }
}
