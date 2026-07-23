#[path = "../askpass.rs"]
mod askpass;

fn main() {
    std::process::exit(askpass::run_native_askpass());
}
