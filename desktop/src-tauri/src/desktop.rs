use crate::backend::{resolve_development_python, Connection, LaunchConfig, Status, Supervisor};
use serde::Deserialize;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::{Emitter, Manager, State};

struct DesktopState {
    supervisor: Arc<Supervisor>,
    allow_exit: Arc<AtomicBool>,
}

#[tauri::command]
fn get_backend_connection(state: State<'_, DesktopState>) -> Result<Connection, String> {
    state.supervisor.connection()
}

#[tauri::command]
fn get_backend_status(state: State<'_, DesktopState>) -> Status {
    state.supervisor.status()
}

#[tauri::command]
fn retry_backend(state: State<'_, DesktopState>) -> Result<(), String> {
    state.supervisor.start()
}

#[tauri::command]
fn request_shutdown(state: State<'_, DesktopState>) {
    state.supervisor.request_exit();
}

#[derive(Deserialize)]
struct EvidenceDirectory {
    directory: PathBuf,
}

#[derive(Deserialize)]
struct EvidenceResponse {
    success: bool,
    data: Option<EvidenceDirectory>,
}

#[tauri::command]
async fn open_evidence_folder(
    session_id: String,
    state: State<'_, DesktopState>,
) -> Result<(), String> {
    let connection = state.supervisor.connection()?;
    if session_id.is_empty() || session_id.len() > 200 || session_id.contains(['/', '\\', '\0']) {
        return Err("周期编号无效。".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        // 目录由鉴权后端按数据库记录解析，网页只能提交周期编号。
        let mut endpoint = reqwest::Url::parse(&format!("{}/records/", connection.base_url))
            .map_err(|_| "本机接口地址无效。")?;
        endpoint
            .path_segments_mut()
            .map_err(|_| "本机接口地址无效。")?
            .pop_if_empty()
            .push(&session_id)
            .push("evidence-directory");
        let client = reqwest::blocking::Client::builder()
            .no_proxy()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .map_err(|error| error.to_string())?;
        let response = client
            .get(endpoint)
            .bearer_auth(&connection.token)
            .send()
            .map_err(|_| "无法读取证据目录，请确认后端仍在运行。")?;
        if !response.status().is_success() {
            return Err(format!(
                "证据目录不可用（HTTP {}）。请刷新记录并检查证据是否存在。",
                response.status()
            ));
        }
        let response: EvidenceResponse = response.json().map_err(|_| "证据目录响应无效。")?;
        let directory = response
            .data
            .filter(|_| response.success)
            .ok_or("证据目录不可用。")?
            .directory;
        if !directory.is_absolute() || !directory.is_dir() {
            return Err("后端返回的证据目录不存在或不是绝对路径。".into());
        }
        let directory = dunce::canonicalize(directory).map_err(|_| "无法访问证据目录。")?;

        // 使用固定系统文件管理器，不接收命令、参数或任意用户路径。
        #[cfg(target_os = "windows")]
        let mut opener = {
            let windows = std::env::var_os("SystemRoot").ok_or("找不到 Windows 系统目录。")?;
            Command::new(PathBuf::from(windows).join("explorer.exe"))
        };
        #[cfg(target_os = "macos")]
        let mut opener = Command::new("/usr/bin/open");
        #[cfg(target_os = "linux")]
        let mut opener = Command::new("xdg-open");
        let mut child = opener
            .arg(directory)
            .spawn()
            .map_err(|error| format!("无法打开系统文件管理器：{error}"))?;
        std::thread::spawn(move || {
            let _ = child.wait();
        });
        Ok(())
    })
    .await
    .map_err(|error| error.to_string())?
}

/// 解析当前运行模式下的后端程序和配置路径。
///
/// Args:
///     app: 当前桌面应用句柄。
///
/// Returns:
///     Ok(LaunchConfig {
///         executable: PathBuf::from("C:/Python312/python.exe"),  // 后端程序路径
///         virtual_environment_executable: Some(PathBuf::from("D:/project/.venv/Scripts/python.exe")),  // 虚拟环境入口
///         arguments: vec!["-m".into(), "src.api".into(), "--desktop".into()],  // 启动参数
///         working_directory: PathBuf::from("D:/project"),  // 后端工作目录
///         config_directory: PathBuf::from("D:/project/config"),  // 业务配置目录
///         ocr_config_path: PathBuf::from("D:/project/src/ocr/config.yaml"),  // OCR 配置路径
///     })
///     Err("...")  // 后端路径或配置解析失败
fn resolve_launch(app: &tauri::AppHandle) -> Result<LaunchConfig, String> {
    // 开发模式使用仓库专属 Python 3.12；发布模式只运行随包提供的 sidecar。
    let (
        executable,
        virtual_environment_executable,
        arguments,
        working_directory,
        default_config,
        default_ocr,
    ) = if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .to_path_buf();
        let (python, virtual_environment_executable) = resolve_development_python(&root)?;
        let config = root.join("config");
        let ocr = root.join("src/ocr/config.yaml");
        (
            python,
            virtual_environment_executable,
            vec!["-m".into(), "src.api".into(), "--desktop".into()],
            root,
            config,
            ocr,
        )
    } else {
        let resources = app
            .path()
            .resource_dir()
            .map_err(|error| error.to_string())?;
        let config = app
            .path()
            .app_config_dir()
            .map_err(|error| error.to_string())?;
        let runtime = resources.join("backend-runtime");
        let binary = if cfg!(target_os = "windows") {
            "beltvision-backend.exe"
        } else {
            "beltvision-backend"
        };
        (
            runtime.join(binary),
            None,
            vec!["--desktop".into()],
            runtime,
            config.clone(),
            config.join("ocr.yaml"),
        )
    };

    // 操作者可在启动应用前指定配置；相对环境路径一律拒绝。
    let config_directory = std::env::var_os("BELTVISION_CONFIG_DIR")
        .map(PathBuf::from)
        .unwrap_or(default_config);
    let ocr_config_path = std::env::var_os("BELTVISION_OCR_CONFIG")
        .map(PathBuf::from)
        .unwrap_or(default_ocr);
    if !config_directory.is_absolute() || !ocr_config_path.is_absolute() {
        return Err("BELTVISION_CONFIG_DIR 和 BELTVISION_OCR_CONFIG 必须使用绝对路径。".into());
    }
    Ok(LaunchConfig {
        executable,
        virtual_environment_executable,
        arguments,
        working_directory,
        config_directory,
        ocr_config_path,
    })
}

/// 启动桌面窗口和本机后端，并在退出时等待资源清理。
///
/// Args:
///     无外部参数。
///
/// Returns:
///     ()  // 桌面事件循环结束，后端已完成退出流程
pub fn run() {
    // Windows 开发版在初始化回调中创建主窗口。
    let context = tauri::generate_context!();
    #[cfg(all(debug_assertions, target_os = "windows"))]
    let context = {
        let mut context = context;
        context.config_mut().app.windows[0].create = false;
        context
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(
            |app, _arguments, _directory| {
                // 重复启动仅聚焦已存在的窗口，不启动第二个后端。
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.unminimize();
                    let _ = window.set_focus();
                }
            },
        ))
        .setup(|app| {
            #[cfg(all(debug_assertions, target_os = "windows"))]
            {
                // 将开发窗口的 WebView2 数据保存到项目运行目录。
                let project_directory = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .unwrap()
                    .parent()
                    .unwrap()
                    .to_path_buf();
                let data_directory = project_directory.join("runtime/desktop-webview2");

                // 使用主窗口配置创建窗口，并在失败时输出缓存路径。
                tauri::WebviewWindowBuilder::from_config(
                    app.handle(),
                    &app.config().app.windows[0],
                )?
                .data_directory(data_directory.clone())
                .build()
                .map_err(|error| {
                    format!(
                        "无法创建桌面窗口（WebView2 数据目录 {}）：{error}",
                        data_directory.display()
                    )
                })?;
            }

            // 创建后端状态通知和退出控制器。
            let handle = app.handle().clone();
            let exit_handle = handle.clone();
            let allow_exit = Arc::new(AtomicBool::new(false));
            let exit_gate = allow_exit.clone();
            let supervisor = Supervisor::create(
                resolve_launch(&handle),
                Arc::new(move |status| {
                    let _ = handle.emit("backend-status", status);
                }),
                Arc::new(move || {
                    exit_gate.store(true, Ordering::SeqCst);
                    exit_handle.exit(0);
                }),
            );

            // 注册桌面状态并启动本机后端。
            app.manage(DesktopState {
                supervisor: supervisor.clone(),
                allow_exit,
            });
            let _ = supervisor.start();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_backend_connection,
            get_backend_status,
            retry_backend,
            request_shutdown,
            open_evidence_folder
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<DesktopState>();
                if !state.allow_exit.load(Ordering::SeqCst) {
                    api.prevent_close();
                    state.supervisor.request_exit();
                }
            }
        })
        .build(context)
        .expect("BeltVision desktop initialization failed")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                let state = app.state::<DesktopState>();
                if !state.allow_exit.load(Ordering::SeqCst) {
                    api.prevent_exit();
                    state.supervisor.request_exit();
                }
            }
        });
}
