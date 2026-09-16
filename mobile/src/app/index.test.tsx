import { render, screen } from "@testing-library/react-native";

import Index from "@/app/index";

describe("Index", () => {
  it("renders the shell's visible content", async () => {
    await render(<Index />);

    expect(screen.getByText("Pulso")).toBeTruthy();
    expect(screen.getByText("Mobile application shell")).toBeTruthy();
    expect(screen.getByText(/^API base URL: /)).toBeTruthy();
  });
});
