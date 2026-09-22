"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "./auth";

export default function Gate({ page, children }) {
  const { user, token, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!ready || !token || !user) return;
    const isParent = user.role === "parent";
    if (isParent) {
      if (page !== "overview" && page !== "advisor") router.replace("/overview");
      return;
    }
    // Students must finish onboarding before anything else opens up. Until they
    // do, /onboarding is the ONLY page — every other route bounces back there.
    if (!user.onboarding_completed) {
      if (page !== "onboarding") router.replace("/onboarding");
    } else if (page === "onboarding") {
      // A completed student has no onboarding tab/flow left — go to the dashboard.
      router.replace("/dashboard");
    }
  }, [ready, token, user, page, router]);

  if (!ready || !token || !user) return null;
  const isParent = user.role === "parent";
  if (isParent) {
    if (page !== "overview" && page !== "advisor") return null;
    return children;
  }
  if (!user.onboarding_completed) {
    if (page !== "onboarding") return null;
    return children;
  }
  if (page === "overview" || page === "advisor") return null;
  return children;
}