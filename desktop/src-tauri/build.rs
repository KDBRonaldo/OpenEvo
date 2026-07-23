fn main() {
    println!("cargo:rerun-if-changed=src/askpass.rs");
    println!("cargo:rerun-if-changed=src/bin/openevo-ssh-askpass.rs");
    tauri_build::build()
}
