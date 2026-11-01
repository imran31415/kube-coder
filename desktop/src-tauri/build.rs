fn main() {
    // Listing the commands makes each one a permission (`allow-kc-request`, …)
    // that a capability must grant. Without this, Tauri lets every window call
    // every app command; with it, only the window the `main` capability names
    // can (capabilities/main.json).
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "kc_item_get",
            "kc_item_set",
            "kc_item_remove",
            "kc_secret_get",
            "kc_secret_set",
            "kc_secret_delete",
            "kc_request",
            "kc_live_status",
            "kc_live_kick",
            "kc_take_targets",
            "kc_open_embed",
        ]),
    ))
    .expect("failed to run tauri-build");
}
