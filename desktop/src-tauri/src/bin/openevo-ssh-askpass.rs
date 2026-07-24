#[path = "../askpass.rs"]
mod askpass;

fn main() {
    let code = if askpass::is_system_ssh_owner_invocation() {
        askpass::run_native_system_ssh_owner()
    } else {
        askpass::run_native_askpass()
    };
    std::process::exit(code);
}
