//! What the tray shows and writes, from `/api/status`: the decision and its reason, the
//! three modes as check items, and the icon's form, ink and size.
use crate::Os;
use serde_json::{json, Value};

/// The UI's own sentence for a core that does not answer.
pub const NOT_ANSWERING: &str = "The core is not answering";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Direct,
    Auto,
    Relay,
}

impl Mode {
    /// In the slider's order, left to right: the menu lists them so.
    pub const ALL: [Mode; 3] = [Mode::Direct, Mode::Auto, Mode::Relay];

    /// The `routing` value, which is also the menu item's id.
    pub fn id(self) -> &'static str {
        match self {
            Mode::Relay => "relay",
            Mode::Auto => "auto",
            Mode::Direct => "direct",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Mode::Relay => "Relay",
            Mode::Auto => "Auto",
            Mode::Direct => "Direct",
        }
    }

    pub fn from_routing(routing: &str) -> Option<Mode> {
        Mode::ALL.into_iter().find(|m| m.id() == routing)
    }

    /// The `PUT /api/settings` body: the mode alone, so Auto returns to the stored threshold.
    pub fn body(self) -> Value {
        json!({ "routing": self.id() })
    }
}

/// The icon's form: solid while the decision cuts, the open line while it passes through.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Form {
    Solid,
    Line,
}

/// A macOS template image, or a drawing in black or white for the bar's colour.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ink {
    Template,
    Black,
    White,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct View {
    pub headline: String,
    pub reason: Option<String>,
    pub checked: Option<Mode>,
    pub modes_enabled: bool,
    pub form: Form,
    /// Auto's item: the threshold it returns to, when the status names one.
    pub auto_label: String,
}

/// The menu for an answer of `/api/status`, or for none.
pub fn view(status: Option<&Value>) -> View {
    let Some((status, decision)) = status.and_then(|s| Some((s, s.get("decision")?))) else {
        return View {
            headline: NOT_ANSWERING.into(),
            reason: None,
            checked: None,
            modes_enabled: false,
            form: Form::Solid,
            auto_label: Mode::Auto.label().into(),
        };
    };
    let cutting = decision.get("relay_on").and_then(Value::as_bool) == Some(true);
    View {
        headline: if cutting {
            "Cutting every request"
        } else {
            "Passing requests through"
        }
        .into(),
        reason: decision
            .get("reason")
            .and_then(Value::as_str)
            .map(String::from),
        checked: status
            .get("routing")
            .and_then(Value::as_str)
            .and_then(Mode::from_routing),
        modes_enabled: true,
        form: if cutting { Form::Solid } else { Form::Line },
        // Only a threshold the core can hold, a whole number from 1 to 99; JSON's 30.0 is a
        // float, not one, and reads as plain "Auto" like any other.
        auto_label: status
            .pointer("/low_quota/threshold_percent")
            .and_then(Value::as_u64)
            .filter(|t| (1..=99).contains(t))
            .map_or_else(
                || Mode::Auto.label().into(),
                |t| format!("{} (below {t} %)", Mode::Auto.label()),
            ),
    }
}

/// Whether choosing `chosen` writes: not the mode already checked, and not while the core
/// is not answering.
pub fn writes(view: &View, chosen: Mode) -> bool {
    view.modes_enabled && view.checked != Some(chosen)
}

/// Windows says whether its taskbar is light; Linux panels say nothing, and GNOME's stays
/// dark under a light preference, so white is the safe ink there.
pub fn ink(os: Os, light_bar: Option<bool>) -> Ink {
    match (os, light_bar) {
        (Os::MacOs, _) => Ink::Template,
        (Os::Windows, Some(true)) => Ink::Black,
        _ => Ink::White,
    }
}

/// The tray PNG's pixel size: the bar's own grid for the primary monitor's scale factor,
/// so the open form's 1.5 px line is never downsampled; a tie takes the larger. macOS takes
/// one 32 px template.
pub fn tray_px(os: Os, scale: f64) -> u32 {
    if os == Os::MacOs {
        return 32;
    }
    let want = 16.0 * scale;
    [32, 24, 20, 16]
        .into_iter()
        .min_by(|a, b| {
            (*a as f64 - want)
                .abs()
                .total_cmp(&(*b as f64 - want).abs())
        })
        .expect("four sizes")
}

/// `reg query …\Themes\Personalize /v SystemUsesLightTheme`'s answer: whether the taskbar
/// is light, or `None` when the output says neither.
pub fn light_taskbar(reg_query_output: &str) -> Option<bool> {
    let line = reg_query_output
        .lines()
        .find(|l| l.trim_start().starts_with("SystemUsesLightTheme"))?;
    match line.split_whitespace().collect::<Vec<_>>()[..] {
        [_, "REG_DWORD", "0x1"] => Some(true),
        [_, "REG_DWORD", "0x0"] => Some(false),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn status(routing: &str, relay_on: bool, reason: &str) -> Value {
        json!({
            "routing": routing,
            "low_quota": { "threshold_percent": 30 },
            "decision": { "relay_on": relay_on, "reason": reason },
        })
    }

    /// Auto passing, with `threshold` as the status's `low_quota.threshold_percent`.
    fn with_threshold(threshold: Value) -> Value {
        json!({
            "routing": "auto",
            "low_quota": { "threshold_percent": threshold },
            "decision": { "relay_on": false, "reason": "no watched account is low" },
        })
    }

    #[test]
    fn the_menu_follows_the_status() {
        let cutting = "Cutting every request";
        let passing = "Passing requests through";
        let at_30 = "Auto (below 30 %)";
        let quiet = Some("no watched account is low");
        // (status, headline, reason, checked, modes enabled, form, Auto's label)
        let rows = [
            (
                Some(status("auto", true, "every watched account is low")),
                cutting,
                Some("every watched account is low"),
                Some(Mode::Auto),
                true,
                Form::Solid,
                at_30,
            ),
            (
                Some(status("direct", false, "routing: direct")),
                passing,
                Some("routing: direct"),
                Some(Mode::Direct),
                true,
                Form::Line,
                at_30,
            ),
            (
                Some(status("relay", true, "routing: relay")),
                cutting,
                Some("routing: relay"),
                Some(Mode::Relay),
                true,
                Form::Solid,
                at_30,
            ),
            (
                Some(status("auto", false, "the quota cannot be read")),
                passing,
                Some("the quota cannot be read"),
                Some(Mode::Auto),
                true,
                Form::Line,
                at_30,
            ),
            (
                Some(json!({
                    "routing": "auto",
                    "low_quota": { "threshold_percent": 45 },
                    "decision": { "relay_on": false, "reason": "no watched account is low" },
                })),
                passing,
                Some("no watched account is low"),
                Some(Mode::Auto),
                true,
                Form::Line,
                "Auto (below 45 %)",
            ),
            // A status that names no threshold.
            (
                Some(json!({
                    "routing": "auto",
                    "decision": { "relay_on": true, "reason": "every watched account is low" },
                })),
                cutting,
                Some("every watched account is low"),
                Some(Mode::Auto),
                true,
                Form::Solid,
                "Auto",
            ),
            // A threshold the core cannot hold (it reads 1 to 99, a whole number) is not said.
            (
                Some(with_threshold(json!(1))),
                passing,
                quiet,
                Some(Mode::Auto),
                true,
                Form::Line,
                "Auto (below 1 %)",
            ),
            (
                Some(with_threshold(json!(99))),
                passing,
                quiet,
                Some(Mode::Auto),
                true,
                Form::Line,
                "Auto (below 99 %)",
            ),
            (
                Some(with_threshold(json!(0))),
                passing,
                quiet,
                Some(Mode::Auto),
                true,
                Form::Line,
                "Auto",
            ),
            (
                Some(with_threshold(json!(500))),
                passing,
                quiet,
                Some(Mode::Auto),
                true,
                Form::Line,
                "Auto",
            ),
            (
                Some(with_threshold(json!(30.0))),
                passing,
                quiet,
                Some(Mode::Auto),
                true,
                Form::Line,
                "Auto",
            ),
            (
                Some(with_threshold(json!(-5))),
                passing,
                quiet,
                Some(Mode::Auto),
                true,
                Form::Line,
                "Auto",
            ),
            (
                Some(with_threshold(json!("30"))),
                passing,
                quiet,
                Some(Mode::Auto),
                true,
                Form::Line,
                "Auto",
            ),
            (None, NOT_ANSWERING, None, None, false, Form::Solid, "Auto"),
            // An answer without a decision is not one the menu can show.
            (
                Some(json!({ "routing": "relay", "low_quota": { "threshold_percent": 30 } })),
                NOT_ANSWERING,
                None,
                None,
                false,
                Form::Solid,
                "Auto",
            ),
            (
                Some(status("sideways", false, "routing: sideways")),
                passing,
                Some("routing: sideways"),
                None,
                true,
                Form::Line,
                at_30,
            ),
        ];
        for (status, headline, reason, checked, modes_enabled, form, auto_label) in rows {
            let want = View {
                headline: headline.into(),
                reason: reason.map(String::from),
                checked,
                modes_enabled,
                form,
                auto_label: auto_label.into(),
            };
            assert_eq!(view(status.as_ref()), want, "{status:?}");
        }
    }

    #[test]
    fn each_mode_writes_the_body_of_spec_3_1() {
        assert_eq!(Mode::Relay.body(), json!({ "routing": "relay" }));
        assert_eq!(Mode::Auto.body(), json!({ "routing": "auto" }));
        assert_eq!(Mode::Direct.body(), json!({ "routing": "direct" }));
        // The menu lists them in the slider's order, left to right.
        assert_eq!(Mode::ALL, [Mode::Direct, Mode::Auto, Mode::Relay]);
        for mode in Mode::ALL {
            assert_eq!(Mode::from_routing(mode.id()), Some(mode));
        }
    }

    #[test]
    fn the_checked_mode_chosen_again_writes_nothing() {
        let auto = view(Some(&status("auto", true, "every watched account is low")));
        let down = view(None);
        // (view, chosen, writes)
        let rows = [
            (&auto, Mode::Auto, false),
            (&auto, Mode::Relay, true),
            (&auto, Mode::Direct, true),
            (&down, Mode::Relay, false),
            (&down, Mode::Auto, false),
            (&down, Mode::Direct, false),
        ];
        for (view, chosen, want) in rows {
            assert_eq!(writes(view, chosen), want, "{chosen:?} on {view:?}");
        }
    }

    #[test]
    fn the_icon_ink_and_size_suit_the_platform() {
        // (os, light bar, ink)
        let inks = [
            (Os::MacOs, Some(true), Ink::Template),
            (Os::MacOs, Some(false), Ink::Template),
            (Os::MacOs, None, Ink::Template),
            (Os::Windows, Some(true), Ink::Black),
            (Os::Windows, Some(false), Ink::White),
            (Os::Windows, None, Ink::White),
            (Os::Linux, Some(true), Ink::White),
            (Os::Linux, None, Ink::White),
        ];
        for (os, light_bar, want) in inks {
            assert_eq!(ink(os, light_bar), want, "{os:?} {light_bar:?}");
        }
        // (os, scale factor, px)
        let sizes = [
            (Os::MacOs, 1.0, 32),
            (Os::MacOs, 2.0, 32),
            (Os::Windows, 1.0, 16),
            (Os::Windows, 1.25, 20),
            (Os::Windows, 1.5, 24),
            (Os::Windows, 2.0, 32),
            (Os::Windows, 1.75, 32),
            (Os::Windows, 3.0, 32),
            (Os::Linux, 1.0, 16),
            (Os::Linux, 1.25, 20),
            (Os::Linux, 1.5, 24),
            (Os::Linux, 2.0, 32),
            (Os::Linux, 1.75, 32),
            (Os::Linux, 3.0, 32),
        ];
        for (os, scale, want) in sizes {
            assert_eq!(tray_px(os, scale), want, "{os:?} {scale}");
        }
    }

    #[test]
    fn the_taskbar_theme_is_read_from_reg_output() {
        let key = r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize";
        let answer = |value: &str| {
            format!("\r\n{key}\r\n    SystemUsesLightTheme    REG_DWORD    {value}\r\n\r\n")
        };
        let rows = [
            (answer("0x1"), Some(true)),
            (answer("0x0"), Some(false)),
            (
                "ERROR: The system was unable to find the specified registry key or value.\r\n"
                    .into(),
                None,
            ),
            (String::new(), None),
        ];
        for (output, want) in rows {
            assert_eq!(light_taskbar(&output), want, "{output:?}");
        }
    }
}
