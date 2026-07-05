import SwiftUI

struct ContentView: View {
    @State private var counter = 0

    var body: some View {
        VStack(spacing: 20) {
            Text("Raven Sample")
                .font(.largeTitle)
                .fontWeight(.bold)

            Text("Button taps: \(counter)")
                .font(.title3)
                .foregroundStyle(.secondary)

            Button("Increment Counter") {
                counter += 1
            }
            .buttonStyle(.borderedProminent)
        }
        .padding()
    }
}
