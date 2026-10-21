// What the Tauri shell may inject (spec 6.4); a plain browser defines nothing.
interface UpdateStatus {
  state: "idle" | "checking" | "up_to_date" | "available" | "downloading" | "installing" | "error";
  current_version: string;
  version: string | null;
  notes_url: string | null;
  can_install: boolean;
  reason: string | null;
  error: string | null;
}
interface Window {
  lokki?: {
    restart?: (service: "core" | "cliproxy") => Promise<void>;
    openExternal?: (url: string) => Promise<void>;
    openPanel?: () => Promise<void>;
    copyManagementSecret?: () => Promise<void>;
    updateStatus?: () => Promise<UpdateStatus>;
    checkForUpdate?: () => Promise<UpdateStatus>;
    installUpdate?: () => Promise<void>;
  };
}
