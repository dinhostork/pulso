import { useIsFocused } from "expo-router";
import {
  createContext,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { AppState } from "react-native";

import type { CardVisibility } from "@/features/feed/visibility";

import { FeedExposureController } from "./exposure";
import type { ImpressionQueue } from "./queue";

const ImpressionQueueContext = createContext<ImpressionQueue | null>(null);

export function ImpressionQueueProvider({
  queue,
  children,
}: {
  queue: ImpressionQueue;
  children: ReactNode;
}) {
  return (
    <ImpressionQueueContext.Provider value={queue}>{children}</ImpressionQueueContext.Provider>
  );
}

function useImpressionQueue(): ImpressionQueue {
  const queue = useContext(ImpressionQueueContext);
  if (queue === null)
    throw new Error("useImpressionQueue must be used inside ImpressionQueueProvider");
  return queue;
}

/** Foreground only while the OS reports the app as active; background and inactive close the gate. */
function useAppActive(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const subscription = AppState.addEventListener("change", onChange);
      return () => subscription.remove();
    },
    () => AppState.currentState === "active",
  );
}

/**
 * Wires the HOME_FEED exposure lifecycle: a feed session per Feed mount and
 * per successful explicit refresh (`refreshGeneration`), the focus/foreground
 * gate, and a best-effort flush when the gate closes. Returns the stable
 * visibility observer for the Feed list; the Feed never waits on delivery.
 */
export function useFeedExposure(refreshGeneration: number): (cards: CardVisibility[]) => void {
  const queue = useImpressionQueue();
  const focused = useIsFocused();
  const active = useAppActive();
  const [controller] = useState(() => new FeedExposureController({ queue }));

  useEffect(() => () => controller.dispose(), [controller]);
  useEffect(() => controller.startSession(), [controller, refreshGeneration]);
  useEffect(() => {
    const open = focused && active;
    controller.setGate(open);
    if (!open) queue.flushNow();
  }, [controller, queue, focused, active]);

  return controller.observe;
}
