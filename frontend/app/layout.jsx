import "./globals.css";

export const metadata = {
  title: {
    default: "amazing-http-logger / Management console",
    template: "%s / amazing-http-logger",
  },
  description:
    "Owner-scoped HTTP traffic analysis and device encryption management.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:rounded focus:bg-white focus:p-3 focus:text-primary"
        >
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
