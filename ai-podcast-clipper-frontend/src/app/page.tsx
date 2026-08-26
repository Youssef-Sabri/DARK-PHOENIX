import { redirect } from "next/navigation";

export default function HomePage() {
  // The app entry point is the dashboard; it redirects unauthenticated
  // users to /login on its own.
  redirect("/dashboard");
}
