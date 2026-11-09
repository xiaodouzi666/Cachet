import type { Metadata } from "next";
import type { ReactNode } from "react";
import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "./globals.css";
import { Shell } from "../components/shell";

export const metadata: Metadata = {
  title: {
    default: "Cachet · Warm context marketplace",
    template: "%s · Cachet",
  },
  description:
    "Publish warm LLM context, meter engine-verified cache hits and settle publisher royalties through PayPal.",
};
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
