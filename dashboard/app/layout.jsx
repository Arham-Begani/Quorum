import "./globals.css";

export const metadata = {
  title: "Quorum — memory consistency for multi-agent systems",
  description:
    "Transactions solve write conflicts. They do not solve semantic conflicts. " +
    "Three-mode comparison on CockroachDB.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
