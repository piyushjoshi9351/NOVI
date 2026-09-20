"use client";
import Gate from "../../../src/Gate";
import OnboardingPage from "../../../src/views/OnboardingPage";

// The live onboarding engine is the flow + voice one (ONBOARDING_ENGINE=legacy),
// served at /onboarding/flow/* with /onboarding/voice/*. The conversational
// engine (OnboardingChat) mounts here only when ONBOARDING_ENGINE=new.
export default function Page() {
  return <Gate page="onboarding"><OnboardingPage /></Gate>;
}