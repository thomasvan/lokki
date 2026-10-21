import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/svelte";

// waitFor and findBy* give up after 1 s by default; on a shared workstation that limit is there to catch a hang, not to time a test.
configure({ asyncUtilTimeout: 10_000 });
