use rand::RngCore;
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Connection {
    pub base_url: String,
    pub token: String,
}

#[derive(Clone, Serialize, Debug, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    pub phase: String,
    pub message: String,
    pub process_running: bool,
    pub retry_allowed: bool,
}

impl Status {
    fn create(phase: &str, message: impl Into<String>, running: bool) -> Self {
        Self {
            phase: phase.into(),
            message: message.into(),
            process_running: running,
            retry_allowed: !running && phase == "failed",
        }
    }
}

#[derive(Clone)]
pub struct LaunchConfig {
    pub executable: PathBuf,
    pub virtual_environment_executable: Option<PathBuf>,
    pub arguments: Vec<String>,
    pub working_directory: PathBuf,
    pub config_directory: PathBuf,
    pub ocr_config_path: PathBuf,
}

/// 解析项目虚拟环境使用的解释器及其启动路径。
///
/// Args:
///     project_directory: 项目根目录的绝对路径。
///
/// Returns:
///     Ok((
///         PathBuf::from("C:/Python312/python.exe"),  // 实际解释器路径
///         Some(PathBuf::from("D:/project/.venv/Scripts/python.exe")),  // 虚拟环境入口
///     ))
///     Err("无法读取 Python 虚拟环境配置：...")  // 配置读取或解析失败
pub fn resolve_development_python(
    project_directory: &Path,
) -> Result<(PathBuf, Option<PathBuf>), String> {
    // 非 Windows 平台直接使用虚拟环境的解释器。
    if !cfg!(target_os = "windows") {
        return Ok((project_directory.join(".venv/bin/python"), None));
    }

    // 从虚拟环境配置中读取实际 Python 安装目录。
    let virtual_environment_directory = project_directory.join(".venv");
    let configuration_path = virtual_environment_directory.join("pyvenv.cfg");
    let configuration = std::fs::read_to_string(&configuration_path).map_err(|error| {
        format!(
            "无法读取 Python 虚拟环境配置 {}：{error}",
            configuration_path.display()
        )
    })?;
    let python_home = configuration
        .lines()
        .filter_map(|line| line.split_once('='))
        .find(|(name, _value)| name.trim() == "home")
        .map(|(_name, value)| PathBuf::from(value.trim()))
        .ok_or("Python 虚拟环境配置缺少 home。")?;

    // 返回实际解释器和虚拟环境入口。
    Ok((
        python_home.join("python.exe"),
        Some(virtual_environment_directory.join("Scripts/python.exe")),
    ))
}

#[derive(Serialize)]
struct Bootstrap<'a> {
    token: &'a str,
    host: &'static str,
    port: u16,
    config_dir: &'a std::path::Path,
    ocr_config_path: &'a std::path::Path,
    parent_pid: u32,
}

#[derive(Deserialize, Debug)]
struct Handshake {
    protocol: u32,
    host: String,
    port: u16,
    pid: u32,
}

struct Inner {
    status: Status,
    connection: Option<Connection>,
    stop_sender: Option<mpsc::Sender<()>>,
    exit_after_stop: bool,
}

type StatusCallback = Arc<dyn Fn(Status) + Send + Sync>;
type ExitCallback = Arc<dyn Fn() + Send + Sync>;

pub struct Supervisor {
    inner: Mutex<Inner>,
    launch: Result<LaunchConfig, String>,
    on_status: StatusCallback,
    on_exit: ExitCallback,
}

impl Supervisor {
    pub fn create(
        launch: Result<LaunchConfig, String>,
        on_status: StatusCallback,
        on_exit: ExitCallback,
    ) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(Inner {
                status: Status::create("starting", "正在启动本机后端…", false),
                connection: None,
                stop_sender: None,
                exit_after_stop: false,
            }),
            launch,
            on_status,
            on_exit,
        })
    }

    pub fn status(&self) -> Status {
        self.inner.lock().unwrap().status.clone()
    }

    pub fn connection(&self) -> Result<Connection, String> {
        self.inner
            .lock()
            .unwrap()
            .connection
            .clone()
            .ok_or_else(|| "后端尚未就绪，请查看连接状态。".into())
    }

    pub fn start(self: &Arc<Self>) -> Result<(), String> {
        // 锁内登记所有权，防止重复点击同时启动两个后端。
        let (sender, receiver) = mpsc::channel();
        {
            let mut inner = self.inner.lock().unwrap();
            if inner.stop_sender.is_some() || inner.exit_after_stop {
                return Err("现有后端尚未退出，不能启动第二个后端。".into());
            }
            inner.stop_sender = Some(sender);
            inner.connection = None;
            inner.status = Status::create("starting", "正在启动本机后端…", true);
        }
        (self.on_status)(self.status());
        let supervisor = self.clone();
        thread::spawn(move || supervisor.run_backend(receiver));
        Ok(())
    }

    pub fn request_exit(&self) {
        // 窗口关闭只发起清理；实际进程退出后才关闭桌面。
        let mut inner = self.inner.lock().unwrap();
        inner.exit_after_stop = true;
        if let Some(sender) = &inner.stop_sender {
            let _ = sender.send(());
        } else {
            drop(inner);
            (self.on_exit)();
        }
    }

    fn publish(&self, status: Status) {
        self.inner.lock().unwrap().status = status.clone();
        (self.on_status)(status);
    }

    fn finish(&self, expected: bool, detail: String) {
        // 清除令牌和进程所有权后才允许重试或退出桌面。
        let (status, should_exit) = {
            let mut inner = self.inner.lock().unwrap();
            inner.connection = None;
            inner.stop_sender = None;
            if !expected {
                inner.exit_after_stop = false;
            }
            inner.status = if expected {
                Status::create("stopped", "后端已完成清理并退出。", false)
            } else {
                Status::create("failed", detail, false)
            };
            (inner.status.clone(), inner.exit_after_stop && expected)
        };
        (self.on_status)(status);
        if should_exit {
            (self.on_exit)();
        }
    }

    fn run_backend(&self, stop_receiver: mpsc::Receiver<()>) {
        // 启动配置来自固定程序路径和操作者环境，不接受网页指定路径。
        let launch = match self.launch.clone() {
            Ok(value) => value,
            Err(message) => {
                self.finish(false, message);
                return;
            }
        };
        if !launch.executable.is_file()
            || !launch.config_directory.join("config.yaml").is_file()
            || !launch.ocr_config_path.is_file()
        {
            self.finish(false, format!(
                "缺少 Python/sidecar 或配置。请核对程序 {}、配置目录 {}、OCR 配置 {} 后重新启动应用。",
                launch.executable.display(), launch.config_directory.display(), launch.ocr_config_path.display()
            ));
            return;
        }

        // 每次启动生成新令牌，仅通过子进程标准输入传递。
        let mut secret = [0_u8; 32];
        rand::rngs::OsRng.fill_bytes(&mut secret);
        let token = secret
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let mut command = Command::new(&launch.executable);
        command
            .args(&launch.arguments)
            .current_dir(&launch.working_directory);
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        command.env("PYTHONUNBUFFERED", "1");
        // 为实际解释器指定项目虚拟环境入口。
        if let Some(executable) = &launch.virtual_environment_executable {
            command.env("__PYVENV_LAUNCHER__", executable);
        }

        // Windows 后端进程不显示控制台窗口。
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = match command.spawn() {
            Ok(value) => value,
            Err(error) => {
                self.finish(
                    false,
                    format!("无法启动后端：{error}。请检查已安装的运行环境。"),
                );
                return;
            }
        };

        // 保持 stdin 存活；桌面异常退出时 EOF 会触发后端的同一清理流程。
        let mut input = child.stdin.take();
        let bootstrap = Bootstrap {
            token: &token,
            host: "127.0.0.1",
            port: 0,
            config_dir: &launch.config_directory,
            ocr_config_path: &launch.ocr_config_path,
            parent_pid: std::process::id(),
        };
        let wrote_bootstrap = input.as_mut().is_some_and(|stdin| {
            serde_json::to_writer(&mut *stdin, &bootstrap).is_ok()
                && stdin.write_all(b"\n").is_ok()
                && stdin.flush().is_ok()
        });
        if !wrote_bootstrap {
            input.take();
            self.publish(Status::create(
                "failed",
                "后端启动通道中断，正在等待进程退出。",
                true,
            ));
        }

        // 只解析启动握手，不把子进程标准输出或令牌转发到网页或日志。
        let output = child.stdout.take().unwrap();
        let (handshake_sender, handshake_receiver) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(output).lines().map_while(Result::ok) {
                if let Ok(handshake) = serde_json::from_str::<Handshake>(&line) {
                    let _ = handshake_sender.send(handshake);
                }
            }
        });
        self.monitor_child(
            &mut child,
            &mut input,
            token,
            handshake_receiver,
            stop_receiver,
            wrote_bootstrap,
        );
    }

    fn monitor_child(
        &self,
        child: &mut Child,
        input: &mut Option<std::process::ChildStdin>,
        token: String,
        handshake_receiver: mpsc::Receiver<Handshake>,
        stop_receiver: mpsc::Receiver<()>,
        wrote_bootstrap: bool,
    ) {
        // HTTP 请求禁止代理，且只连接经过握手校验的 IPv4 回环地址。
        let client = Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(3))
            .build()
            .unwrap();
        let started = Instant::now();
        let mut connection: Option<Connection> = None;
        let mut stopping = !wrote_bootstrap;
        let mut shutdown_sent = false;
        let mut startup_failed = !wrote_bootstrap;
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    let detail = if startup_failed {
                        format!(
                            "{} 后端现已退出（{status}），处理启动问题后可重试。",
                            self.status().message
                        )
                    } else {
                        format!("后端已退出（{status}）。请核对配置、Paddle/MVS 环境或日志后重试；异常终止不能保证未完成数据已保存。")
                    };
                    self.finish(stopping && status.success() && !startup_failed, detail);
                    return;
                }
                Err(error) => {
                    self.publish(Status::create(
                        "failed",
                        format!("无法确认后端退出：{error}。禁止启动第二个实例。"),
                        true,
                    ));
                    thread::sleep(Duration::from_secs(1));
                    continue;
                }
                Ok(None) => {}
            }
            if stop_receiver.try_recv().is_ok() && !stopping {
                stopping = true;
                self.publish(Status::create(
                    "stopping",
                    "正在等待采集、OCR、保存与审计完成；完成前窗口不会退出。",
                    true,
                ));
            }

            // 握手 PID 必须是本窗口直接启动的进程。
            if connection.is_none() && !startup_failed {
                if let Ok(handshake) = handshake_receiver.try_recv() {
                    match validate_handshake(&handshake, child.id()) {
                        Ok(base_url) => {
                            let candidate = Connection {
                                base_url,
                                token: token.clone(),
                            };
                            let ready = client
                                .get(format!("{}/health", candidate.base_url))
                                .bearer_auth(&candidate.token)
                                .send()
                                .is_ok_and(|response| response.status().is_success());
                            if ready {
                                self.inner.lock().unwrap().connection = Some(candidate.clone());
                                connection = Some(candidate);
                                if !stopping {
                                    self.publish(Status::create("ready", "本机后端已连接。", true));
                                }
                            } else {
                                startup_failed = true;
                                stopping = true;
                                input.take();
                                self.publish(Status::create(
                                    "failed",
                                    "后端健康检查失败，已请求清理，等待进程退出后才能重试。",
                                    true,
                                ));
                            }
                        }
                        Err(message) => {
                            startup_failed = true;
                            stopping = true;
                            input.take();
                            self.publish(Status::create("failed", message, true));
                        }
                    }
                } else if started.elapsed() > Duration::from_secs(90) {
                    startup_failed = true;
                    stopping = true;
                    input.take();
                    self.publish(Status::create(
                        "failed",
                        "后端 90 秒内未就绪，已请求清理；仍等待实际退出，不会强杀或重复启动。",
                        true,
                    ));
                }
            }

            // 关闭请求先走鉴权接口；通信失败时通过 EOF 请求相同清理，绝不按固定期限强杀。
            if stopping && !shutdown_sent {
                if let Some(connection) = &connection {
                    let response = client
                        .post(format!("{}/shutdown", connection.base_url))
                        .bearer_auth(&connection.token)
                        .send();
                    if !response.is_ok_and(|value| value.status().is_success()) {
                        self.publish(Status::create(
                            "stopping",
                            "关闭接口未响应，已通过父进程通道请求清理；仍等待后端实际退出。",
                            true,
                        ));
                    }
                    input.take();
                    shutdown_sent = true;
                } else {
                    input.take();
                    shutdown_sent = true;
                }
            }
            thread::sleep(Duration::from_millis(150));
        }
    }
}

fn validate_handshake(handshake: &Handshake, child_pid: u32) -> Result<String, String> {
    if handshake.protocol != 1
        || handshake.host != "127.0.0.1"
        || handshake.port == 0
        || handshake.pid != child_pid
    {
        return Err("后端启动握手无效，已请求清理；不能连接未知进程。".into());
    }
    Ok(format!("http://127.0.0.1:{}/api/v1", handshake.port))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_bound_loopback_child() {
        let handshake = Handshake {
            protocol: 1,
            host: "127.0.0.1".into(),
            port: 43123,
            pid: 42,
        };
        assert_eq!(
            validate_handshake(&handshake, 42).unwrap(),
            "http://127.0.0.1:43123/api/v1"
        );
        assert!(validate_handshake(&handshake, 43).is_err());
    }

    #[test]
    fn rejects_external_host_and_unbound_port() {
        for (host, port, protocol) in [
            ("0.0.0.0", 43123, 1),
            ("127.0.0.1", 0, 1),
            ("127.0.0.1", 43123, 2),
        ] {
            assert!(validate_handshake(
                &Handshake {
                    host: host.into(),
                    port,
                    protocol,
                    pid: 42
                },
                42
            )
            .is_err());
        }
    }

    #[test]
    fn retry_requires_observed_exit() {
        assert!(!Status::create("failed", "still cleaning", true).retry_allowed);
        assert!(Status::create("failed", "exited", false).retry_allowed);
    }

    /// 验证 Windows 项目后端的握手及有序退出。
    ///
    /// Args:
    ///     无外部参数。
    ///
    /// Returns:
    ///     ()  // 后端完成 PID 校验、健康检查和退出
    #[cfg(target_os = "windows")]
    #[test]
    fn starts_project_python_and_waits_for_exit() {
        // 读取当前项目的根目录。
        let project_directory = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .to_path_buf();

        // 创建独立的后端测试目录。
        let directory =
            std::env::temp_dir().join(format!("belt-windows-supervisor-{}", rand::random::<u64>()));
        std::fs::create_dir_all(&directory).unwrap();

        // 写入使用临时数据库的后端配置。
        std::fs::write(
            directory.join("config.yaml"),
            "application:\n  database_path: data.sqlite3\n  evidence_directory: evidence\n\
             camera:\n  mvs_development_directory: sdk\nocr: {}\nfrequency: {}\nmachine: {}\n\
             io:\n  modbus_serial_port: COM8\n  io_machine_channels: {}\n",
        )
        .unwrap();
        std::fs::write(directory.join("ocr.yaml"), "{}").unwrap();

        // 使用桌面的同一解释器配置启动真实后端。
        let (executable, virtual_environment_executable) =
            resolve_development_python(&project_directory).unwrap();
        let (exit_sender, exit_receiver) = mpsc::channel();
        let supervisor = Supervisor::create(
            Ok(LaunchConfig {
                executable,
                virtual_environment_executable,
                arguments: vec!["-m".into(), "src.api".into(), "--desktop".into()],
                working_directory: project_directory,
                config_directory: directory.clone(),
                ocr_config_path: directory.join("ocr.yaml"),
            }),
            Arc::new(|_| {}),
            Arc::new(move || {
                let _ = exit_sender.send(());
            }),
        );
        supervisor.start().unwrap();

        // 等待后端完成启动。
        let deadline = Instant::now() + Duration::from_secs(15);
        while supervisor.status().phase == "starting" && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        let status = supervisor.status();

        // 请求后端退出并等待进程结束。
        supervisor.request_exit();
        exit_receiver.recv_timeout(Duration::from_secs(15)).unwrap();

        // 检查握手和退出结果。
        assert_eq!(status.phase, "ready", "{}", status.message);
        assert_eq!(supervisor.status().phase, "stopped");
        assert!(!supervisor.status().process_running);
        assert!(supervisor.connection().is_err());

        // 清理独立测试目录。
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn waits_for_cleanup_and_actual_child_exit() {
        // 测试子进程只访问临时目录，不加载真实配置或硬件。
        let directory =
            std::env::temp_dir().join(format!("belt-supervisor-{}", rand::random::<u64>()));
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(directory.join("config.yaml"), "{}").unwrap();
        std::fs::write(directory.join("ocr.yaml"), "{}").unwrap();
        std::fs::write(directory.join("child.py"), r#"
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
bootstrap = json.loads(sys.stdin.readline())
stop = threading.Event()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200 if self.headers.get('Authorization') == 'Bearer ' + bootstrap['token'] else 401)
        self.end_headers()
    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + bootstrap['token']:
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(202)
        self.end_headers()
        stop.set()
server = HTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(json.dumps(dict(protocol=1, host='127.0.0.1', port=server.server_port, pid=os.getpid())), flush=True)
stop.wait()
time.sleep(0.8)
Path('cleanup-completed').write_text('saved and audited')
server.shutdown()
"#).unwrap();
        let (exit_sender, exit_receiver) = mpsc::channel();
        let supervisor = Supervisor::create(
            Ok(LaunchConfig {
                executable: PathBuf::from("/usr/bin/python3"),
                virtual_environment_executable: None,
                arguments: vec![directory.join("child.py").to_string_lossy().into_owned()],
                working_directory: directory.clone(),
                config_directory: directory.clone(),
                ocr_config_path: directory.join("ocr.yaml"),
            }),
            Arc::new(|_| {}),
            Arc::new(move || {
                let _ = exit_sender.send(());
            }),
        );
        supervisor.start().unwrap();
        let deadline = Instant::now() + Duration::from_secs(8);
        while supervisor.status().phase != "ready" && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        assert_eq!(supervisor.status().phase, "ready");
        assert!(supervisor.start().is_err());
        supervisor.request_exit();
        assert!(exit_receiver
            .recv_timeout(Duration::from_millis(250))
            .is_err());
        assert!(supervisor.status().process_running);
        exit_receiver.recv_timeout(Duration::from_secs(8)).unwrap();
        assert!(!supervisor.status().process_running);
        assert_eq!(supervisor.status().phase, "stopped");
        assert_eq!(
            std::fs::read_to_string(directory.join("cleanup-completed")).unwrap(),
            "saved and audited"
        );
        assert!(supervisor.connection().is_err());
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn crash_does_not_claim_clean_exit_and_allows_retry() {
        // 非零退出只能报告故障，不发出完成清理的桌面退出通知。
        let directory = std::env::temp_dir().join(format!("belt-crash-{}", rand::random::<u64>()));
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(directory.join("config.yaml"), "{}").unwrap();
        std::fs::write(directory.join("ocr.yaml"), "{}").unwrap();
        let (exit_sender, exit_receiver) = mpsc::channel();
        let supervisor = Supervisor::create(
            Ok(LaunchConfig {
                executable: PathBuf::from("/usr/bin/python3"),
                virtual_environment_executable: None,
                arguments: vec![
                    "-c".into(),
                    "import sys; sys.stdin.readline(); sys.exit(2)".into(),
                ],
                working_directory: directory.clone(),
                config_directory: directory.clone(),
                ocr_config_path: directory.join("ocr.yaml"),
            }),
            Arc::new(|_| {}),
            Arc::new(move || {
                let _ = exit_sender.send(());
            }),
        );
        for _ in 0..2 {
            supervisor.start().unwrap();
            let deadline = Instant::now() + Duration::from_secs(5);
            while supervisor.status().process_running && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(20));
            }
            let status = supervisor.status();
            assert_eq!(status.phase, "failed");
            assert!(status.retry_allowed);
            assert!(!status.process_running);
            assert!(exit_receiver.try_recv().is_err());
        }
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn status_never_serializes_connection_secret() {
        let status = Status::create("ready", "connected", true);
        assert!(!serde_json::to_string(&status).unwrap().contains("token"));
    }
}
