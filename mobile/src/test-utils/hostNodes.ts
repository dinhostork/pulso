import { screen } from "expo-router/testing-library";

export type HostNode = ReturnType<typeof screen.getByTestId>;

/** Every rendered host element, for controls whose mocks expose no label or test ID. */
export function hostNodes(): HostNode[] {
  const nodes: HostNode[] = [];
  const visit = (node: HostNode) => {
    nodes.push(node);
    for (const child of node.children) {
      if (typeof child === "object" && child !== null) visit(child as HostNode);
    }
  };
  visit(screen.root as HostNode);
  return nodes;
}

/** The list's RefreshControl host; `fireEvent(…, "refresh")` reaches its `onRefresh`. */
export function refreshControl(): HostNode {
  const control = hostNodes().find((node) => node.type === "RCTRefreshControl");
  if (!control) throw new Error("No refresh control is rendered");
  return control;
}
