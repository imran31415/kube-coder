//! The tray icon and the "needs you" badge.
//!
//! Each OS shows a count differently, so the same number goes everywhere it
//! can: the dock badge on macOS, the launcher badge on Linux desktops that
//! support one (Unity launcher API: Ubuntu Dock, KDE, Plank), a red overlay
//! dot on the Windows taskbar button (Windows has no numeric badge API for
//! desktop apps), and the tray tooltip — plus the menu-bar title on macOS.

#[cfg_attr(target_os = "macos", allow(unused_imports))]
use crate::core::{show_main, MAIN};
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
#[cfg_attr(target_os = "macos", allow(unused_imports))]
use tauri::{AppHandle, Manager};

const TRAY_ID: &str = "kc-tray";

pub fn install(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open kube-coder", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip("kube-coder")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

pub fn label(count: usize) -> String {
    match count {
        0 => "kube-coder".into(),
        n => format!("kube-coder \u{2014} {n} need{} you", if n == 1 { "s" } else { "" }),
    }
}

pub fn set_needs_you(app: &AppHandle, count: usize) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_tooltip(Some(label(count)));
        #[cfg(target_os = "macos")]
        let _ = tray.set_title(if count > 0 { Some(count.to_string()) } else { None });
    }
    // The macOS dock tile belongs to the application, not to a window, so it
    // is set directly: the window may have been dropped (unload.rs) and the
    // badge must still be right.
    #[cfg(target_os = "macos")]
    {
        let badge = (count > 0).then(|| count.to_string());
        let _ = app.run_on_main_thread(move || set_dock_badge(badge));
    }
    // Linux and Windows set theirs through the window; when it is closed they
    // are set again as it is rebuilt (`show_main`).
    #[cfg(not(target_os = "macos"))]
    {
        let Some(window) = app.get_webview_window(MAIN) else { return };
        #[cfg(target_os = "linux")]
        let _ = window.set_badge_count(if count > 0 { Some(count as i64) } else { None });
        #[cfg(windows)]
        let _ = window.set_overlay_icon(if count > 0 { Some(red_dot()) } else { None });
        #[cfg(not(any(target_os = "linux", windows)))]
        let _ = window;
    }
}

/// `NSApp.dockTile.badgeLabel`, which is all tao's `set_badge_count` does,
/// minus the window. Main thread only, which `run_on_main_thread` gives it.
#[cfg(target_os = "macos")]
fn set_dock_badge(label: Option<String>) {
    use objc2::MainThreadMarker;
    use objc2_app_kit::NSApplication;
    use objc2_foundation::NSString;
    let Some(mtm) = MainThreadMarker::new() else { return };
    let label = label.map(|l| NSString::from_str(&l));
    NSApplication::sharedApplication(mtm).dockTile().setBadgeLabel(label.as_deref());
}

/// A 16×16 red dot for the Windows taskbar overlay, drawn here rather than
/// shipped as a file.
#[cfg_attr(not(windows), allow(dead_code))]
fn red_dot() -> Image<'static> {
    const N: u32 = 16;
    let mut rgba = Vec::with_capacity((N * N * 4) as usize);
    let c = (N as f32 - 1.0) / 2.0;
    for y in 0..N {
        for x in 0..N {
            let d = ((x as f32 - c).powi(2) + (y as f32 - c).powi(2)).sqrt();
            let alpha = ((c + 0.5 - d).clamp(0.0, 1.0) * 255.0) as u8;
            rgba.extend_from_slice(&[0xE5, 0x48, 0x4D, alpha]);
        }
    }
    Image::new_owned(rgba, N, N)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_read_naturally() {
        assert_eq!(label(0), "kube-coder");
        assert_eq!(label(1), "kube-coder \u{2014} 1 needs you");
        assert_eq!(label(3), "kube-coder \u{2014} 3 need you");
    }

    #[test]
    fn the_overlay_dot_is_a_full_icon() {
        let img = red_dot();
        assert_eq!((img.width(), img.height()), (16, 16));
        assert_eq!(img.rgba().len(), 16 * 16 * 4);
    }
}
