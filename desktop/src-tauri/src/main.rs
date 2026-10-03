#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

/// 启动 BeltVision 桌面主流程。
///
/// Args:
///     无外部参数。
///
/// Returns:
///     ()  // 桌面主流程已结束
fn main() {
    beltvision_desktop_library::run();
}
