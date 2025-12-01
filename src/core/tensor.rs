pub struct Tensor {
    data: Vec<f32>,
    size: Vec<usize>,
    strides: Vec<usize>,
}

// row major order
impl Tensor {
    pub fn new(data: &[f32], size: &[usize]) -> Tensor {
        let mut strides = vec![1; size.len()];
        for i in (0..size.len().saturating_sub(1)).rev() {
            strides[i] = strides[i + 1] * size[i + 1];
        }
        Tensor {
            data: data.to_vec(),
            size: size.to_vec(),
            strides,
        }
    }

    pub fn get(&self, index: &[usize]) -> f32 {
        let mut offset = 0;
        for i in 0..index.len() {
            offset += index[i] * self.strides[i];
        }
        self.data[offset]
    }

    pub fn set(&mut self, index: &[usize], value: f32){
        let mut offset = 0;
        for i in 0..index.len() {
            offset += index[i] * self.strides[i];
        }
        self.data[offset] = value;
    }

    pub fn add(&self, other: &Tensor) -> Tensor {
        assert_eq!(self.size, other.size, "Tensor shapes must match");

        let data = self.data.iter()
        .zip(other.data.iter())
        .map(|(&a, &b)| a + b)
        .collect();

        Tensor {
            data,
            size: self.size.clone(),
            strides: self.strides.clone(),
        }
    }

    pub fn sub(&self, other: &Tensor) -> Tensor {
        assert_eq!(self.size, other.size, "Tensor shapes must match");
        let data = self.data.iter()
        .zip(other.data.iter())
        .map(|(&a, &b)| a - b)
        .collect();
        Tensor {
            data,
            size: self.size.clone(),
            strides: self.strides.clone(),
        }
    }

    pub fn relu(&self) -> Tensor {
        let data = self.data.iter()
        .map(|&x| x.max(0.0))
        .collect();

        Tensor {
            data,
            size: self.size.clone(),
            strides: self.strides.clone(),
        }
    }

    pub fn softmax(&self, usemax: bool) -> Tensor {
        let mut max = 0.0;
        if usemax {
            max = self.data.iter().max().unwrap();
        }
        let mut data =  self.data.iter().map(|&x| (x - max).exp()).collect();
        let sum = data.iter().sum();
        data = data.iter().map(|&x| x / sum).collect();
        Tensor::new(&data, &self.size)
    }
    
    pub fn matmul(&self, other: &Tensor) -> Tensor { // need to optimize this for SIMD and CPU cache optimizations

        assert_eq!(self.size[1], other.size[0], "Matrix dimensions must match");
        let mut result = vec![0.0; self.size[0] * other.size[1]];
        for i in 0..self.size[0] {
            for j in 0..other.size[1]{
                let mut sum = 0.0;
                for k in 0..self.size[1]{
                    sum += self.get(&[i, k]) * other.get(&[k, j]);
                }
                result[i * other.size[1] + j] = sum;
            }
        } 
        
        Tensor::new(&result, &[self.size[0], other.size[1]])

    }

}