import { IBM_Plex_Mono, IBM_Plex_Sans, Newsreader } from "next/font/google";
import "./globals.css";

/* Three voices, deliberately distinct:
 *   Newsreader     — the argument. Editorial serif, used for the thesis and for
 *                    the marginal notes that tell you what you are looking at.
 *   IBM Plex Sans  — the instrument. Every figure, label and stat tile.
 *                    Data figures never wear a serif; that reads as decoration.
 *   IBM Plex Mono  — the record. Subject keys, claims, modes, identifiers —
 *                    anything that exists verbatim in the database.
 */
const display = Newsreader({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  style: ["normal", "italic"],
  variable: "--font-display",
  display: "swap",
});

const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata = {
  title: "Quorum — memory consistency for multi-agent systems",
  description:
    "Transactions solve write conflicts. They do not solve semantic conflicts. " +
    "Three-mode comparison on CockroachDB.",
};

export default function RootLayout({ children }) {
  return (
    <html
      lang="en"
      className={`${display.variable} ${sans.variable} ${mono.variable}`}
    >
      <body>
        <div className="grain" aria-hidden="true" />
        {children}
      </body>
    </html>
  );
}
