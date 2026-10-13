import React from "react";
import { formatDate } from "../lib/format";

export class Home extends React.Component {
  render() {
    return <h1>Welcome, today is {formatDate(new Date())}</h1>;
  }
}
