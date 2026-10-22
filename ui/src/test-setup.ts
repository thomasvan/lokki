import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/svelte";

// waitFor and findBy* give up after 1 s by default. The suite runs on a shared workstation and on CI's
// hosted runners, both slower than a quiet machine, so this limit is there to catch a hang, not to time a test.
configure({ asyncUtilTimeout: 10_000 });
