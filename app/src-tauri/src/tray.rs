//! The tray: the core's decision and its reason, the three modes as check items, open and
//! quit. The icon is the mark, solid while the decision cuts and the open line while it
//! passes through. What it shows is `lokki_logic::tray`'s; this file puts it on screen.
use crate::Shell;
use lokki_logic::tray::{self, Form, Ink, Mode, View};
use lokki_logic::Os;
use std::sync::{Arc, Mutex};
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::{AppHandle, Manager, Wry};

pub const OPEN: &str = "Open lokki";
pub const QUIT: &str = "Quit lokki";

/// The icon as drawn: its form, ink and pixel size.
type Look = (Form, Ink, u32);

pub struct Tray {
    icon: TrayIcon,
    menu: Menu<Wry>,
    headline: MenuItem<Wry>,
    reason: MenuItem<Wry>,
    /// In `Mode::ALL`'s order.
    modes: [CheckMenuItem<Wry>; 3],
    /// Held only by the threads that apply a view, never by the event loop, which each
    /// change below waits on.
    shown: Mutex<Shown>,
}

struct Shown {
    reason: bool,
    checked: Option<Mode>,
    look: Look,
}

impl Tray {
    /// Every item's text (Auto's included), enabled and checked state from `view`, set every time because a
    /// click toggles a check item by itself; the icon only when its look changes.
    pub fn apply(&self, view: &View, ink: Ink, px: u32) -> tauri::Result<()> {
        let mut shown = self.shown.lock().unwrap();
        self.headline.set_text(&view.headline)?;
        // A menu item cannot be hidden: the reason leaves the menu while there is none.
        match (&view.reason, shown.reason) {
            (Some(reason), was) => {
                self.reason.set_text(reason)?;
                if !was {
                    self.menu.insert(&self.reason, 1)?;
                    shown.reason = true;
                }
            }
            (None, true) => {
                self.menu.remove(&self.reason)?;
                shown.reason = false;
            }
            (None, false) => {}
        }
        for (item, mode) in self.modes.iter().zip(Mode::ALL) {
            if mode == Mode::Auto {
                item.set_text(&view.auto_label)?;
            }
            item.set_enabled(view.modes_enabled)?;
            item.set_checked(view.checked == Some(mode))?;
        }
        shown.checked = view.checked;
        let look = (view.form, ink, px);
        if shown.look != look {
            self.icon
                .set_icon_with_as_template(Some(image(look)), ink == Ink::Template)?;
            shown.look = look;
        }
        Ok(())
    }

    /// The check marks as last applied, before anything reads the core: a click toggles
    /// its item by itself, and a core slow to answer would leave that toggle standing.
    pub fn recheck(&self) -> tauri::Result<()> {
        let shown = self.shown.lock().unwrap();
        for (item, mode) in self.modes.iter().zip(Mode::ALL) {
            item.set_checked(shown.checked == Some(mode))?;
        }
        Ok(())
    }
}

/// The ink and pixel size for the bar the icon sits on. It reads the primary monitor, which
/// waits on the event loop, and on Windows runs `reg`: call it off the event loop.
pub fn bar(app: &AppHandle) -> (Ink, u32) {
    let os = Os::current();
    let scale = app
        .primary_monitor()
        .ok()
        .flatten()
        .map_or(1.0, |m| m.scale_factor());
    (tray::ink(os, light_taskbar()), tray::tray_px(os, scale))
}

/// Whether the Windows taskbar is light, from the setting the taskbar itself follows
/// (`AppsUseLightTheme` is the apps', which differs under Windows 10's default).
#[cfg(windows)]
fn light_taskbar() -> Option<bool> {
    let out = crate::process::command("reg")
        .args([
            "query",
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            "/v",
            "SystemUsesLightTheme",
        ])
        .output()
        .ok()?;
    tray::light_taskbar(&String::from_utf8_lossy(&out.stdout))
}

#[cfg(not(windows))]
fn light_taskbar() -> Option<bool> {
    None
}

/// A template is drawn black: macOS reads only its alpha.
fn image((form, ink, px): Look) -> Image<'static> {
    use Form::{Line, Solid};
    match (form, ink == Ink::White, px) {
        (Solid, false, 16) => tauri::include_image!("icons/tray-solid-black-16.png"),
        (Solid, false, 20) => tauri::include_image!("icons/tray-solid-black-20.png"),
        (Solid, false, 24) => tauri::include_image!("icons/tray-solid-black-24.png"),
        (Solid, false, _) => tauri::include_image!("icons/tray-solid-black-32.png"),
        (Solid, true, 16) => tauri::include_image!("icons/tray-solid-white-16.png"),
        (Solid, true, 20) => tauri::include_image!("icons/tray-solid-white-20.png"),
        (Solid, true, 24) => tauri::include_image!("icons/tray-solid-white-24.png"),
        (Solid, true, _) => tauri::include_image!("icons/tray-solid-white-32.png"),
        (Line, false, 16) => tauri::include_image!("icons/tray-line-black-16.png"),
        (Line, false, 20) => tauri::include_image!("icons/tray-line-black-20.png"),
        (Line, false, 24) => tauri::include_image!("icons/tray-line-black-24.png"),
        (Line, false, _) => tauri::include_image!("icons/tray-line-black-32.png"),
        (Line, true, 16) => tauri::include_image!("icons/tray-line-white-16.png"),
        (Line, true, 20) => tauri::include_image!("icons/tray-line-white-20.png"),
        (Line, true, 24) => tauri::include_image!("icons/tray-line-white-24.png"),
        (Line, true, _) => tauri::include_image!("icons/tray-line-white-32.png"),
    }
}

/// The menu before the core has answered. The icon's ink and size are guessed here, on the
/// event loop, without reading the monitor or the registry; the first refresh corrects them.
pub fn build(app: &AppHandle) -> tauri::Result<Tray> {
    let first = tray::view(None);
    let headline = MenuItem::with_id(app, "headline", &first.headline, false, None::<&str>)?;
    let reason = MenuItem::with_id(app, "reason", "", false, None::<&str>)?;
    let [direct, auto, relay] = Mode::ALL
        .map(|m| CheckMenuItem::with_id(app, m.id(), m.label(), false, false, None::<&str>));
    let modes = [direct?, auto?, relay?];
    let open = MenuItem::with_id(app, "open", OPEN, true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", QUIT, true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &headline,
            &PredefinedMenuItem::separator(app)?,
            &modes[0],
            &modes[1],
            &modes[2],
            &PredefinedMenuItem::separator(app)?,
            &open,
            &quit,
        ],
    )?;
    let os = Os::current();
    let look = (first.form, tray::ink(os, None), tray::tray_px(os, 1.0));
    let icon = TrayIconBuilder::with_id("lokki")
        .tooltip("lokki")
        .icon(image(look))
        .icon_as_template(look.1 == Ink::Template)
        .menu(&menu)
        .on_menu_event(|app, event| {
            let shell = Arc::clone(&app.state::<Arc<Shell>>());
            let id = event.id().as_ref();
            // Each reaches the core over HTTP: off the event loop.
            if let Some(mode) = Mode::from_routing(id) {
                std::thread::spawn(move || shell.choose(mode));
                return;
            }
            match id {
                "open" => crate::window::show_main(app),
                "quit" => {
                    std::thread::spawn(move || shell.quit());
                }
                _ => {}
            }
        })
        .build(app)?;
    Ok(Tray {
        icon,
        menu,
        headline,
        reason,
        modes,
        shown: Mutex::new(Shown {
            reason: false,
            checked: first.checked,
            look,
        }),
    })
}
