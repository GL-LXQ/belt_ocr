fn main() {
    #[cfg(feature = "desktop")]
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "get_backend_connection",
            "get_backend_status",
            "retry_backend",
            "request_shutdown",
            "open_evidence_folder",
        ]),
    ))
    .expect("Tauri build configuration failed");
}
