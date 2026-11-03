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
mod unload;

use crate::core::{accept_deep_link, show_main, Core, LiveStatus, Target, MAIN};
use std::sync::Arc;
use tauri::{AppHandle, Manager, State, Webview, WindowEvent};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_notification::NotificationExt;

type CoreState<'a> = State<'a, Arc<Core>>;

/// Every command is for the bundled UI only. Capabilities already scope them
/// to the `main` window; this is the same rule again, in code, so a remote
/// page in an embed window can never reach them by any route.
fn main_only(webview: &Webview, cmd: &str) -> Result<(), String> {
    if std::env::var_os("KC_DESKTOP_TRACE").is_some() {
        // Which command, from which window — never its arguments.
        eprintln!("[kc] {cmd} from {}", webview.label());
    }
    if webview.label() == MAIN {
        Ok(())
    } else {
        Err("not allowed from this window".into())
    }
}

#[tauri::command]
fn kc_item_get(webview: Webview, core: CoreState, key: String) -> Result<Option<String>, String> {
    main_only(&webview, "kc_item_get")?;
    Ok(core.item_get(&key))
}

#[tauri::command]
fn kc_item_set(webview: Webview, core: CoreState, key: String, value: String) -> Result<(), String> {
    main_only(&webview, "kc_item_set")?;
    core.item_set(&key, &value)
}

#[tauri::command]
fn kc_item_remove(webview: Webview, core: CoreState, key: String) -> Result<(), String> {
    main_only(&webview, "kc_item_remove")?;
    core.item_remove(&key)
}

// Keychain calls block (Secret Service is a D-Bus round trip; macOS may show
// an access prompt), so they run off the main thread.
#[tauri::command(async)]
fn kc_secret_get(webview: Webview, core: CoreState, key: String) -> Result<Option<String>, String> {
    main_only(&webview, "kc_secret_get")?;
    core.secret_get(&key)
}

#[tauri::command(async)]
fn kc_secret_set(webview: Webview, core: CoreState, key: String, value: String) -> Result<(), String> {
    main_only(&webview, "kc_secret_set")?;
    core.secret_set(&key, &value)
}

#[tauri::command(async)]
fn kc_secret_delete(webview: Webview, core: CoreState, key: String) -> Result<(), String> {
    main_only(&webview, "kc_secret_delete")?;
    core.secret_delete(&key)
}

#[tauri::command]
async fn kc_request(webview: Webview, core: CoreState<'_>, req: http::WsRequest) -> Result<http::WsResponse, String> {
    main_only(&webview, "kc_request")?;
    core.request(req).await
}

#[tauri::command]
fn kc_live_status(webview: Webview, core: CoreState) -> Result<LiveStatus, String> {
    main_only(&webview, "kc_live_status")?;
    Ok(core.status())
}

#[tauri::command]
fn kc_live_kick(webview: Webview, core: CoreState) -> Result<(), String> {
    main_only(&webview, "kc_live_kick")?;
    core.kick();
    Ok(())
}

#[tauri::command]
fn kc_take_targets(webview: Webview, core: CoreState) -> Result<Vec<Target>, String> {
    main_only(&webview, "kc_take_targets")?;
    Ok(core.take_targets())
}

#[tauri::command]
async fn kc_open_embed(
    webview: Webview,
    core: CoreState<'_>,
    next: String,
    title: String,
) -> Result<(), String> {
    main_only(&webview, "kc_open_embed")?;
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
                        if *f {
                            // Focused means on screen, however it got there.
                            core.on_shown();
                        }
                    }
                }
                // Closing the window keeps the app in the tray, still
                // watching for things that need you. Quit is in the tray menu.
                WindowEvent::CloseRequested { api, .. } => {
                    if let Some(core) = window.try_state::<Arc<Core>>() {
                        core.on_focus(false);
                    }
                    if !close_to_tray() {
                        api.prevent_close();
                        let _ = window.hide();
                        unload_when_hidden(window.app_handle());
                    }
                }
                _ => {}
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building kube-coder desktop")
        .run(|_app, event| match event {
            // The last window closing is not quitting: the core keeps running
            // in the tray. Only an explicit exit (the tray's Quit) ends it.
            tauri::RunEvent::ExitRequested { api, code: None, .. } => api.prevent_exit(),
            // A click on the dock icon with no window on screen: open it,
            // rebuilding it if it was dropped while hidden.
            #[cfg(target_os = "macos")]
            tauri::RunEvent::Reopen { has_visible_windows: false, .. } => show_main(_app),
            _ => {}
        });
}

/// After the window has stayed hidden for `KC_DESKTOP_UNLOAD_AFTER_MIN`
/// minutes, destroy it, and the webview with it (unload.rs). The core keeps
/// the stream, the alerts and the badge; `show_main` rebuilds the window.
fn unload_when_hidden(app: &AppHandle) {
    let Some(core) = app.try_state::<Arc<Core>>() else { return };
    let Some((ticket, wait)) = core.on_hidden() else { return };
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(wait).await;
        let Some(core) = app.try_state::<Arc<Core>>() else { return };
        let Some(window) = app.get_webview_window(MAIN) else { return };
        if window.is_visible().unwrap_or(true) || !core.take_unload(ticket) {
            return;
        }
        if let Err(e) = window.destroy() {
            eprintln!("[kc] could not drop the hidden window: {e}");
        }
    });
}

/// Whether closing the window destroys it rather than hiding it.
///
/// The Rust core raises the alerts and sets the badge on its own, so a closed
/// window does not need a live webview behind it — and the webview is nearly
/// all of the app's memory (desktop/README.md). On Linux and Windows the
/// window is destroyed, leaving only the core in the tray; it is rebuilt when
/// opened again. macOS hides it instead, because a Mac app that stays in the
/// dock is expected to come back as it was left, and drops it only after it
/// has stayed hidden a while (`unload_when_hidden`).
fn close_to_tray() -> bool {
    !cfg!(target_os = "macos")
}
