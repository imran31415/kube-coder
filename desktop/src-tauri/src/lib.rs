//! kube-coder desktop: a native shell around the mobile app's web build.
//!
//! The UI is `mobile/`'s `expo export --platform web`, bundled into the app
//! (see desktop/README.md). This crate is everything a browser tab cannot do:
//! keep the token in the OS keychain, hold the workspace's event stream with a
//! Bearer header, raise native notifications, badge the dock, answer
//! `kubecoder://` links and stay a single instance.

mod core;
mod embed;
mod feed;
mod http;
mod scope;
mod secrets;
mod settings;
mod sse;
mod tray;

use crate::core::{accept_deep_link, show_main, Core, LiveStatus, Target, MAIN};
use std::sync::Arc;
use tauri::{Manager, State, Webview, WindowEvent};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_notification::NotificationExt;

type CoreState<'a> = State<'a, Arc<Core>>;

/// Every command is for the bundled UI only. Capabilities already scope them
/// to the `main` window; this is the same rule again, in code, so a remote
/// page in an embed window can never reach them by any route.
fn main_only(webview: &Webview) -> Result<(), String> {
    if webview.label() == MAIN {
        Ok(())
    } else {
        Err("not allowed from this window".into())
    }
}

#[tauri::command]
fn kc_item_get(webview: Webview, core: CoreState, key: String) -> Result<Option<String>, String> {
    main_only(&webview)?;
    Ok(core.item_get(&key))
}

#[tauri::command]
fn kc_item_set(webview: Webview, core: CoreState, key: String, value: String) -> Result<(), String> {
    main_only(&webview)?;
    core.item_set(&key, &value)
}

#[tauri::command]
fn kc_item_remove(webview: Webview, core: CoreState, key: String) -> Result<(), String> {
    main_only(&webview)?;
    core.item_remove(&key)
}

// Keychain calls block (Secret Service is a D-Bus round trip; macOS may show
// an access prompt), so they run off the main thread.
#[tauri::command(async)]
fn kc_secret_get(webview: Webview, core: CoreState, key: String) -> Result<Option<String>, String> {
    main_only(&webview)?;
    core.secret_get(&key)
}

#[tauri::command(async)]
fn kc_secret_set(webview: Webview, core: CoreState, key: String, value: String) -> Result<(), String> {
    main_only(&webview)?;
    core.secret_set(&key, &value)
}

#[tauri::command(async)]
fn kc_secret_delete(webview: Webview, core: CoreState, key: String) -> Result<(), String> {
    main_only(&webview)?;
    core.secret_delete(&key)
}

#[tauri::command]
async fn kc_request(webview: Webview, core: CoreState<'_>, req: http::WsRequest) -> Result<http::WsResponse, String> {
    main_only(&webview)?;
    core.request(req).await
}

#[tauri::command]
fn kc_live_status(webview: Webview, core: CoreState) -> Result<LiveStatus, String> {
    main_only(&webview)?;
    Ok(core.status())
}

#[tauri::command]
fn kc_live_kick(webview: Webview, core: CoreState) -> Result<(), String> {
    main_only(&webview)?;
    core.kick();
    Ok(())
}

#[tauri::command]
fn kc_take_targets(webview: Webview, core: CoreState) -> Result<Vec<Target>, String> {
    main_only(&webview)?;
    Ok(core.take_targets())
}

#[tauri::command]
async fn kc_open_embed(
    webview: Webview,
    core: CoreState<'_>,
    next: String,
    title: String,
) -> Result<(), String> {
    main_only(&webview)?;
    embed::open(webview.app_handle(), &core, &next, &title).await
}

pub fn run() {
    tauri::Builder::default()
        // First, so a second launch is caught before anything else starts. The
        // deep-link feature forwards its URL to on_open_url below.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| show_main(app)))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            kc_item_get,
            kc_item_set,
            kc_item_remove,
            kc_secret_get,
            kc_secret_set,
            kc_secret_delete,
            kc_request,
            kc_live_status,
            kc_live_kick,
            kc_take_targets,
            kc_open_embed,
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            let core = Core::new(handle.clone(), app.path().app_config_dir()?, app.path().app_data_dir()?);
            app.manage(core.clone());

            tray::install(&handle)?;

            // A notification click ("tap") opens what it was about.
            let for_notes = core.clone();
            let _ = app.notification().on_action(move |action| {
                if let Some(n) = action.notification() {
                    for_notes.on_notification_action(n.id());
                }
            });

            // kubecoder:// links: the one that launched us, and later ones
            // (including those a second launch forwards).
            #[cfg(target_os = "linux")]
            if std::env::var_os("APPIMAGE").is_some() {
                let _ = app.deep_link().register_all();
            }
            if let Ok(Some(urls)) = app.deep_link().get_current() {
                for u in urls {
                    if let Some(t) = accept_deep_link(u.as_str()) {
                        core.push_target(t);
                    }
                }
            }
            let for_links = core.clone();
            app.deep_link().on_open_url(move |event| {
                for u in event.urls() {
                    if let Some(t) = accept_deep_link(u.as_str()) {
                        for_links.push_target(t);
                    }
                }
            });

            tauri::async_runtime::spawn(core.run());
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != MAIN {
                return;
            }
            match event {
                WindowEvent::Focused(f) => {
                    if let Some(core) = window.try_state::<Arc<Core>>() {
                        core.on_focus(*f);
                    }
                }
                // Closing the window keeps the app in the tray, still
                // watching for things that need you. Quit is in the tray menu.
                WindowEvent::CloseRequested { api, .. } => {
                    api.prevent_close();
                    let _ = window.hide();
                    if let Some(core) = window.try_state::<Arc<Core>>() {
                        core.on_focus(false);
                    }
                }
                _ => {}
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running kube-coder desktop");
}
