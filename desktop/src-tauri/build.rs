fn main() {
  println!("cargo:rerun-if-changed=../dist/index.html");
  tauri_build::build()
}
