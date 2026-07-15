// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AppleFixture",
    platforms: [.iOS(.v16)],
    products: [.library(name: "AppleFixture", targets: ["AppleFixture"])],
    targets: [.target(name: "AppleFixture")]
)
