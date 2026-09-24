import { fireEvent, render, screen } from "@testing-library/react-native";

import { SignInScreen } from "../components/SignInScreen";

async function renderSignIn(
  state: Parameters<typeof SignInScreen>[0]["state"] = { status: "signed_out", reason: "initial" },
) {
  const onSubmit = jest.fn(async () => undefined);
  const onRetryCleanup = jest.fn(async () => true);
  await render(<SignInScreen state={state} onSubmit={onSubmit} onRetryCleanup={onRetryCleanup} />);
  return { onSubmit, onRetryCleanup };
}

describe("SignInScreen", () => {
  it("labels its fields and keeps submission disabled until both are filled", async () => {
    await renderSignIn();
    expect(screen.getByRole("header", { name: "Sign in to Pulso" })).toBeTruthy();
    expect(screen.getByLabelText("Username")).toBeTruthy();
    expect(screen.getByLabelText("Password").props.secureTextEntry).toBe(true);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("submits trimmed credentials and clears the password field", async () => {
    const { onSubmit } = await renderSignIn();
    await fireEvent.changeText(screen.getByLabelText("Username"), " reader ");
    await fireEvent.changeText(screen.getByLabelText("Password"), "secret");
    await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));

    expect(onSubmit).toHaveBeenCalledWith("reader", "secret");
    expect(screen.getByLabelText("Password").props.value).toBe("");
  });

  it("moves from username to password and submits from the keyboard", async () => {
    const { onSubmit } = await renderSignIn();
    const username = screen.getByLabelText("Username");
    const password = screen.getByLabelText("Password");
    expect(username.props.returnKeyType).toBe("next");
    expect(username.props.submitBehavior).toBe("submit");
    expect(password.props.returnKeyType).toBe("go");

    await fireEvent.changeText(username, "reader");
    await fireEvent(username, "submitEditing");
    expect(onSubmit).not.toHaveBeenCalled();
    await fireEvent.changeText(password, "secret");
    await fireEvent(password, "submitEditing");

    expect(onSubmit).toHaveBeenCalledWith("reader", "secret");
  });

  it("shows one generic alert for rejected credentials", async () => {
    await renderSignIn({
      status: "sign_in_error",
      error: { code: "invalid_credentials", retryable: true },
    });
    expect(screen.getByRole("alert")).toHaveTextContent("The username or password is incorrect.");
  });

  it("marks submission busy while signing in", async () => {
    await renderSignIn({ status: "signing_in" });
    expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled();
    expect(screen.getByLabelText("Username").props.editable).toBe(false);
  });

  it("explains residual validity after sign-out and an unconfirmed revocation", async () => {
    await renderSignIn({
      status: "signed_out",
      reason: "logout",
      notice: { code: "remote_logout_failed", retryable: false },
    });
    const notice = screen.getByText(/server could not confirm/);
    expect(notice).toHaveTextContent(/up to 14 days/);
    expect(notice).toHaveTextContent(/up to 15 minutes/);
  });

  it("offers retry when the device could not delete its stored session", async () => {
    const { onRetryCleanup } = await renderSignIn({
      status: "signed_out",
      reason: "logout",
      notice: { code: "credential_delete_failed", retryable: true },
    });
    await fireEvent.press(screen.getByRole("button", { name: "Retry removing stored session" }));
    expect(onRetryCleanup).toHaveBeenCalledTimes(1);
  });
});
