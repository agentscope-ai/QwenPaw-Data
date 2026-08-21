import { message } from "@/design";
import type { TFunction } from "i18next";

export function copyToClipboard(
  text: string,
  t: TFunction,
  successKey: string = "kgBrowser.copiedToClipboard",
): void {
  if (!navigator.clipboard) {
    // Fallback for non-HTTPS or older browsers
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    try {
      document.execCommand("copy");
      message.success(t(successKey));
    } catch (err) {
      console.error("Clipboard write failed:", err);
      message.error(t("kgBrowser.copyFailed"));
    } finally {
      document.body.removeChild(textarea);
    }
    return;
  }
  navigator.clipboard
    .writeText(text)
    .then(() => {
      message.success(t(successKey));
    })
    .catch((err) => {
      console.error("Clipboard write failed:", err);
      message.error(t("kgBrowser.copyFailed"));
    });
}
