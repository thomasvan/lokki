// The wall clock for a screen that draws times seen from now (a countdown, "within the last day"). It
// follows the clock and not only the reads: the core answers every 10 to 30 s, and a reset or a day's turn
// can fall between two answers. `reads` names the state whose change also takes the time again; call it
// while the component initialises.
import { onMount } from "svelte";

export const CLOCK_MS = 10_000;
const now = () => Math.floor(Date.now() / 1000);

export function useNow(reads: () => void = () => {}): { readonly s: number } {
  let s = $state(now());
  onMount(() => {
    const tick = setInterval(() => { s = now(); }, CLOCK_MS);
    return () => clearInterval(tick);
  });
  $effect(() => { reads(); s = now(); });
  return { get s() { return s; } };
}
