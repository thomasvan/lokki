// A release build is a GUI-subsystem program on Windows: it opens no console window.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    lokki_app::run()
}
